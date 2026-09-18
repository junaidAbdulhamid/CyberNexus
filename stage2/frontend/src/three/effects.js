/**
 * Scene furniture: the things that make the map feel like a place rather than a
 * scatter plot. Each is cheap by construction — a fixed budget of geometry that
 * does not grow with the size of the network.
 */
import * as THREE from "three";
import { hexToInt } from "../lib/theme.js";

/** A soft round sprite. Default GL points are squares, which read as debris. */
let sharedDotTexture = null;
export function dotTexture() {
  if (sharedDotTexture) return sharedDotTexture;
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  gradient.addColorStop(0, "rgba(255,255,255,1)");
  gradient.addColorStop(0.35, "rgba(255,255,255,0.85)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);
  sharedDotTexture = new THREE.CanvasTexture(canvas);
  return sharedDotTexture;
}

/** A soft radial glow on the ground plane, so the network sits on something. */
export function createHorizonGlow(radius, colorHex) {
  const size = 512;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  const { r, g, b } = new THREE.Color(colorHex);
  const rgb = `${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}`;
  gradient.addColorStop(0, `rgba(${rgb}, 0.55)`);
  gradient.addColorStop(0.45, `rgba(${rgb}, 0.16)`);
  gradient.addColorStop(1, `rgba(${rgb}, 0)`);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const mesh = new THREE.Mesh(
    new THREE.PlaneGeometry(radius * 4.2, radius * 4.2),
    new THREE.MeshBasicMaterial({
      map: texture, transparent: true, depthWrite: false,
      blending: THREE.AdditiveBlending, opacity: 0.9,
    })
  );
  mesh.rotation.x = -Math.PI / 2;
  mesh.renderOrder = -2;
  return mesh;
}

/**
 * A grid that fades out with distance instead of ending in a hard square edge.
 * Vertex colours do the fade, so it stays a single draw call.
 */
export function createFadingGrid(radius, colorHex, accentHex) {
  const divisions = 40;
  const size = radius * 3.2;
  const step = size / divisions;
  const half = size / 2;
  const positions = [];
  const colors = [];
  const base = new THREE.Color(colorHex);
  const accent = new THREE.Color(accentHex);

  const push = (x1, z1, x2, z2, color) => {
    for (const [x, z] of [[x1, z1], [x2, z2]]) {
      positions.push(x, 0, z);
      // Fade with radial distance from the centre of the plane.
      const fade = Math.max(0, 1 - Math.hypot(x, z) / half);
      const shade = fade * fade;
      colors.push(color.r * shade, color.g * shade, color.b * shade);
    }
  };

  for (let i = 0; i <= divisions; i += 1) {
    const offset = -half + i * step;
    const color = i % 10 === 0 ? accent : base;
    push(offset, -half, offset, half, color);
    push(-half, offset, half, offset, color);
  }

  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
  const grid = new THREE.LineSegments(
    geometry,
    new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.3 })
  );
  grid.renderOrder = -1;
  return grid;
}

/** A dim starfield, purely for depth cues while orbiting. */
export function createStarfield(radius, count = 900) {
  const positions = new Float32Array(count * 3);
  const sizes = new Float32Array(count);
  for (let i = 0; i < count; i += 1) {
    // Shell, not ball: stars belong behind the network, never inside it.
    const r = radius * (3.5 + Math.random() * 2.5);
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.cos(phi) * 0.45;
    positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
    sizes[i] = Math.random() * 1.6 + 0.4;
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute("size", new THREE.Float32BufferAttribute(sizes, 1));
  const material = new THREE.PointsMaterial({
    color: 0x9fb6d4, size: radius * 0.012, sizeAttenuation: true,
    map: dotTexture(), transparent: true, opacity: 0.5, depthWrite: false,
  });
  const stars = new THREE.Points(geometry, material);
  stars.renderOrder = -3;
  return stars;
}

/**
 * Packets travelling along the links that touch an alerting host.
 *
 * One `Points` object with a fixed capacity, recycled: a particle reaching the
 * end of its link is reassigned to another active link rather than allocated
 * again. Budgeted, so a hundred simultaneous incidents cost the same as one.
 */
export class TrafficFlow {
  constructor(capacity = 420) {
    this.capacity = capacity;
    this.positions = new Float32Array(capacity * 3);
    this.colors = new Float32Array(capacity * 3);
    this.progress = new Float32Array(capacity);
    this.speed = new Float32Array(capacity);
    this.linkIndex = new Int32Array(capacity).fill(-1);
    this.active = 0;
    this.segments = [];

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(this.positions, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(this.colors, 3));
    this.points = new THREE.Points(geometry, new THREE.PointsMaterial({
      size: 0.9, vertexColors: true, transparent: true, opacity: 0.95,
      map: dotTexture(), alphaTest: 0.01,
      blending: THREE.AdditiveBlending, depthWrite: false, sizeAttenuation: true,
    }));
    this.points.frustumCulled = false;
    this.points.renderOrder = 4;
  }

  /** `segments` is [{ from: Vec3ish, to: Vec3ish, color: THREE.Color }]. */
  setSegments(segments, pointSize = 0.9) {
    this.segments = segments;
    this.points.material.size = pointSize;
    if (!segments.length) {
      this.active = 0;
      this.points.geometry.setDrawRange(0, 0);
      return;
    }
    // Enough particles to read as flow, capped so a storm cannot blow the budget.
    this.active = Math.min(this.capacity, segments.length * 6);
    for (let i = 0; i < this.active; i += 1) {
      if (this.linkIndex[i] < 0 || this.linkIndex[i] >= segments.length) {
        this._assign(i, Math.random());
      }
    }
    this.points.geometry.setDrawRange(0, this.active);
  }

  _assign(i, startProgress = 0) {
    const index = Math.floor(Math.random() * this.segments.length);
    this.linkIndex[i] = index;
    this.progress[i] = startProgress;
    this.speed[i] = 0.35 + Math.random() * 0.45;
    const { color } = this.segments[index];
    this.colors[i * 3] = color.r;
    this.colors[i * 3 + 1] = color.g;
    this.colors[i * 3 + 2] = color.b;
  }

  update(dt) {
    if (!this.active || !this.segments.length) return;
    for (let i = 0; i < this.active; i += 1) {
      this.progress[i] += this.speed[i] * dt;
      if (this.progress[i] >= 1) this._assign(i, 0);
      const segment = this.segments[this.linkIndex[i]];
      if (!segment) { this._assign(i, 0); continue; }
      const t = this.progress[i];
      this.positions[i * 3] = segment.from.x + (segment.to.x - segment.from.x) * t;
      this.positions[i * 3 + 1] = segment.from.y + (segment.to.y - segment.from.y) * t;
      this.positions[i * 3 + 2] = segment.from.z + (segment.to.z - segment.from.z) * t;
    }
    this.points.geometry.attributes.position.needsUpdate = true;
    this.points.geometry.attributes.color.needsUpdate = true;
  }

  dispose() {
    this.points.geometry.dispose();
    this.points.material.dispose();
  }
}

/**
 * Expanding rings marking the instant an alert escalates.
 *
 * A fixed pool of eight. The point is the *onset* — motion at the moment of
 * change is the strongest cue there is for drawing the eye to something new, and
 * it costs one ring rather than a permanent animation.
 */
export class Shockwaves {
  constructor(pool = 8) {
    this.pool = [];
    const geometry = new THREE.RingGeometry(0.82, 1.0, 48);
    for (let i = 0; i < pool; i += 1) {
      const mesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
        transparent: true, opacity: 0, side: THREE.DoubleSide,
        blending: THREE.AdditiveBlending, depthWrite: false,
      }));
      mesh.visible = false;
      mesh.renderOrder = 5;
      this.pool.push({ mesh, life: 0, duration: 1, maxScale: 1 });
    }
    this.group = new THREE.Group();
    for (const entry of this.pool) this.group.add(entry.mesh);
  }

  emit(position, colorHex, maxScale = 6, duration = 1.1) {
    const entry = this.pool.find((e) => e.life <= 0)
      || this.pool.reduce((a, b) => (a.life > b.life ? a : b));
    entry.mesh.position.set(position.x, position.y, position.z);
    entry.mesh.material.color.set(colorHex);
    entry.mesh.visible = true;
    entry.life = duration;
    entry.duration = duration;
    entry.maxScale = maxScale;
  }

  update(dt, cameraQuaternion) {
    for (const entry of this.pool) {
      if (entry.life <= 0) continue;
      entry.life -= dt;
      const t = 1 - Math.max(entry.life, 0) / entry.duration;
      const eased = 1 - (1 - t) * (1 - t);          // ease-out
      const scale = 0.4 + eased * entry.maxScale;
      entry.mesh.scale.setScalar(scale);
      entry.mesh.material.opacity = Math.max(0, (1 - t) * 0.85);
      entry.mesh.quaternion.copy(cameraQuaternion);
      if (entry.life <= 0) entry.mesh.visible = false;
    }
  }

  dispose() {
    for (const entry of this.pool) entry.mesh.material.dispose();
    this.pool[0]?.mesh.geometry.dispose();
  }
}

/** A rotating bracket reticle marking the selected host. */
export function createReticle(colorHex) {
  const group = new THREE.Group();
  const material = new THREE.LineBasicMaterial({
    color: hexToInt(colorHex), transparent: true, opacity: 0.95,
  });

  // Four corner brackets.
  const bracket = (sx, sy) => {
    const points = [
      new THREE.Vector3(sx * 1.0, sy * 0.55, 0),
      new THREE.Vector3(sx * 1.0, sy * 1.0, 0),
      new THREE.Vector3(sx * 0.55, sy * 1.0, 0),
    ];
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    return new THREE.Line(geometry, material);
  };
  for (const [sx, sy] of [[1, 1], [1, -1], [-1, 1], [-1, -1]]) group.add(bracket(sx, sy));

  const ringGeometry = new THREE.RingGeometry(1.32, 1.38, 64, 1, 0, Math.PI * 1.35);
  const ring = new THREE.Mesh(ringGeometry, new THREE.MeshBasicMaterial({
    color: hexToInt(colorHex), transparent: true, opacity: 0.55,
    side: THREE.DoubleSide, depthWrite: false,
  }));
  group.add(ring);
  group.userData.ring = ring;
  group.visible = false;
  group.renderOrder = 6;
  return group;
}
