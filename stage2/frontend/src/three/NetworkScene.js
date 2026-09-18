/**
 * The 3D map.
 *
 * ## Performance model
 *
 * **One draw call per object class.** Nodes are a single `InstancedMesh`, links
 * a single `LineSegments`, halos a second `InstancedMesh`, traffic a single
 * `Points`. 5,000 hosts cost the GPU about what 400 do, and changing a node's
 * colour is a buffer write rather than a scene-graph mutation.
 *
 * Instance buffers are rebuilt only when something structural changes —
 * topology, filter, alert state, LOD level — and at most once per frame. The
 * per-frame work is limited to pulsing the handful of nodes actually alerting,
 * advancing the traffic particles, and ageing the shockwaves.
 *
 * ## Why it looks the way it does
 *
 * Alerting nodes glow because of a bloom pass, not because they are drawn
 * larger in a brighter colour. Glow is a *pre-attentive* cue: the eye is drawn
 * to a bright region of the visual field before any conscious search begins, and
 * bloom is how you make a region genuinely brighter rather than merely
 * differently coloured. Per-instance emissive intensity is injected into the
 * standard material with a small `onBeforeCompile` patch, so one instanced draw
 * call yields both shaded geometry and selective glow.
 *
 * ## The rule that overrides everything
 *
 * **An alerting node is always drawn individually**, at full size, whatever the
 * LOD level or the active filter. A visualisation that can hide the thing you
 * are looking for is worse than a list.
 */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { EffectComposer } from "three/examples/jsm/postprocessing/EffectComposer.js";
import { RenderPass } from "three/examples/jsm/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/examples/jsm/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/examples/jsm/postprocessing/OutputPass.js";
import { SEVERITY_STYLE, getPalette, hexToInt, isAlerting } from "../lib/theme.js";
import {
  Shockwaves, TrafficFlow, createFadingGrid, createHorizonGlow, createReticle, createStarfield,
} from "./effects.js";

const NODE_BASE_RADIUS = 0.62;
const CLUSTER_MIN_MEMBERS = 8;
//: Most nodes that may pulse at once. Past a handful, motion stops being a
//: pop-out cue and becomes noise — see the MTTI remediation notes.
const MAX_ANIMATED_NODES = 6;
const SEVERITY_RANK_LOOKUP = { none: 0, info: 1, low: 2, medium: 3, high: 4, critical: 5 };

export { SEVERITY_RANK_LOOKUP };

export class NetworkScene {
  constructor(canvas, { paletteName = "default", onSelect = null, onHover = null,
                        quality = "auto" } = {}) {
    this.canvas = canvas;
    this.palette = getPalette(paletteName);
    this.onSelect = onSelect || (() => {});
    this.onHover = onHover || (() => {});

    this.topology = null;
    this.states = new Map();
    this.filter = { subnets: null, vendors: null, deviceTypes: null, minSeverity: null, query: "" };
    this.instanceToNode = [];
    this.pulsing = [];
    this.selectedId = null;
    this.hoveredId = null;
    this.lodEnabled = true;
    this.lodActive = false;
    this.clusters = [];
    this.frame = 0;
    this.lastFrameTimes = [];
    this.qualityMode = quality;
    this.effectsEnabled = true;
    this.stats = {
      fps: 0, drawCalls: 0, nodesRendered: 0, clusters: 0, triangles: 0,
      animated: 0, suppressed: 0, quality: "high",
    };

    this._initRenderer();
    this._initScene();
    this._initPostProcessing();
    this._initPicking();
    this._bindEvents();
    this._animate = this._animate.bind(this);
    this.running = true;
    this.clock = new THREE.Clock();
    requestAnimationFrame(this._animate);
  }

  // -- setup -----------------------------------------------------------
  _initRenderer() {
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas, antialias: true, powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(this.canvas.clientWidth || 1, this.canvas.clientHeight || 1, false);
    this.renderer.setClearColor(hexToInt(this.palette.background), 1);
    // ACES keeps the emissive highlights from clipping to flat white before the
    // bloom pass has had a chance to spread them.
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.05;
    // EffectComposer calls renderer.render() once per pass, and each call resets
    // the render stats — so reading them afterwards reports the final fullscreen
    // quad and nothing else. Reset manually at the top of the frame instead, and
    // the counter then means what the status bar claims it means: every draw
    // call issued this frame, post-processing included.
    this.renderer.info.autoReset = false;
  }

  _initScene() {
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.Fog(hexToInt(this.palette.fog), 120, 400);

    const aspect = (this.canvas.clientWidth || 16) / (this.canvas.clientHeight || 9);
    this.camera = new THREE.PerspectiveCamera(52, aspect, 0.5, 4000);
    this.camera.position.set(70, 62, 70);

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.075;
    this.controls.rotateSpeed = 0.75;
    this.controls.maxPolarAngle = Math.PI * 0.495;   // never go under the floor
    this.controls.maxDistance = 600;
    this.controls.minDistance = 4;

    this.scene.add(new THREE.AmbientLight(0xdce8ff, 0.55));
    const key = new THREE.DirectionalLight(0xffffff, 1.35);
    key.position.set(60, 140, 70);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x4f7fff, 0.55);
    rim.position.set(-70, -10, -60);
    this.scene.add(rim);

    this.decor = new THREE.Group();
    this.scene.add(this.decor);

    this.traffic = new TrafficFlow();
    this.scene.add(this.traffic.points);
    this.shockwaves = new Shockwaves();
    this.scene.add(this.shockwaves.group);
    this.reticle = createReticle(this.palette.accent);
    this.scene.add(this.reticle);

    this.nodeMesh = null;
    this.haloMesh = null;
    this.linkLines = null;
    this.clusterMesh = null;
    this.labelSprites = [];
  }

  _initPostProcessing() {
    const width = this.canvas.clientWidth || 1;
    const height = this.canvas.clientHeight || 1;
    this.composer = new EffectComposer(this.renderer);
    this.composer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.composer.setSize(width, height);
    this.composer.addPass(new RenderPass(this.scene, this.camera));

    // Threshold is deliberately high: only genuinely emissive geometry should
    // bloom. Too low and the whole scene hazes over, which reads as a smeared
    // screen rather than as glowing hosts.
    this.bloomPass = new UnrealBloomPass(
      new THREE.Vector2(width, height),
      this.palette.bloom ?? 0.7,   // strength
      0.55,                        // radius
      0.68                         // luminance threshold
    );
    this.composer.addPass(this.bloomPass);
    this.composer.addPass(new OutputPass());
  }

  _initPicking() {
    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    this.raycaster.params.Line.threshold = 0.6;
  }

  _bindEvents() {
    this._onResize = () => this.resize();
    window.addEventListener("resize", this._onResize);

    this._onPointerMove = (event) => {
      const rect = this.canvas.getBoundingClientRect();
      this.pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      this.pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      this._pickPending = true;
    };
    this.canvas.addEventListener("pointermove", this._onPointerMove);

    this._onClick = () => {
      const hit = this.pick();
      this.onSelect(hit ? hit.id : null);
    };
    this.canvas.addEventListener("click", this._onClick);
  }

  resize() {
    const width = this.canvas.clientWidth;
    const height = this.canvas.clientHeight;
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.composer?.setSize(width, height);
    this.bloomPass?.setSize(width, height);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  setPalette(paletteName) {
    this.palette = getPalette(paletteName);
    this.renderer.setClearColor(hexToInt(this.palette.background), 1);
    this.scene.fog.color.setHex(hexToInt(this.palette.fog));
    if (this.bloomPass) this.bloomPass.strength = this.palette.bloom ?? 0.7;
    this.reticle.children.forEach((child) => child.material.color.set(this.palette.accent));
    if (this.topology) {
      this._buildDecor();
      this._buildLabels();
    }
    this.requestRebuild();
  }

  /** Bloom and the decorative layers cost GPU; allow them to be switched off. */
  setEffects(enabled) {
    this.effectsEnabled = enabled;
    this.decor.visible = enabled;
    this.traffic.points.visible = enabled;
    this.shockwaves.group.visible = enabled;
    this.stats.quality = enabled ? "high" : "performance";
  }

  // -- data ------------------------------------------------------------
  setTopology(topology) {
    this.topology = topology;
    this.topology.boundsRadius = Math.max(topology.bounds?.radius || 40, 10);
    this._fitSceneScale();
    this._buildDecor();
    this._buildClusters();
    this._buildLabels();
    this._allocateMeshes();
    this.frameAll();
    this.rebuild();
  }

  setStates(states) {
    this.states = states instanceof Map ? states : new Map(Object.entries(states || {}));
    this.requestRebuild();
  }

  updateState(nodeId, state) {
    const previous = this.states.get(nodeId);
    if (!state || state.severity === "none") this.states.delete(nodeId);
    else this.states.set(nodeId, state);

    // A shockwave marks the *onset* of an escalation, which is the moment worth
    // drawing the eye to. Repeats of an alert already on the board do not fire.
    const rank = state ? SEVERITY_RANK_LOOKUP[state.severity] || 0 : 0;
    const previousRank = previous ? SEVERITY_RANK_LOOKUP[previous.severity] || 0 : 0;
    if (this.effectsEnabled && rank >= 3 && rank > previousRank && this.topology) {
      const index = this.topology.index.get(nodeId);
      if (index !== undefined) {
        const node = this.topology.nodes[index];
        this.shockwaves.emit(
          node.position, this.palette.severity[state.severity],
          Math.max(this.topology.boundsRadius * 0.05, 4), 1.2
        );
      }
    }
    this.requestRebuild();
  }

  setFilter(filter) {
    this.filter = { ...this.filter, ...filter };
    this.requestRebuild();
  }

  setSelected(nodeId) {
    this.selectedId = nodeId;
    this.requestRebuild();
  }

  setLod(enabled) {
    this.lodEnabled = enabled;
    this.requestRebuild();
  }

  _fitSceneScale() {
    const radius = this.topology.boundsRadius;
    this.scene.fog.near = radius * 1.7;
    this.scene.fog.far = radius * 5.4;
    this.camera.far = radius * 24;
    this.camera.near = Math.max(radius / 500, 0.1);
    this.camera.updateProjectionMatrix();
    this.controls.maxDistance = radius * 6;
    this.controls.minDistance = Math.max(radius / 60, 1.5);
  }

  _buildDecor() {
    while (this.decor.children.length) {
      const child = this.decor.children.pop();
      child.geometry?.dispose();
      child.material?.map?.dispose();
      child.material?.dispose();
    }
    const radius = this.topology.boundsRadius;
    const bounds = this.topology.bounds;
    const floorY = (bounds?.min.y ?? 0) - radius * 0.1;

    const grid = createFadingGrid(radius, this.palette.grid, this.palette.gridAccent);
    grid.position.set(bounds?.center.x || 0, floorY, bounds?.center.z || 0);
    this.decor.add(grid);

    const glow = createHorizonGlow(radius, this.palette.horizon);
    glow.position.set(bounds?.center.x || 0, floorY - 0.4, bounds?.center.z || 0);
    this.decor.add(glow);

    const stars = createStarfield(radius);
    stars.position.set(bounds?.center.x || 0, bounds?.center.y || 0, bounds?.center.z || 0);
    this.decor.add(stars);
  }

  _buildClusters() {
    const groups = new Map();
    this.topology.nodes.forEach((node, index) => {
      const key = node.subnet || "unknown";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(index);
    });
    this.clusters = [...groups.entries()]
      .filter(([, members]) => members.length >= CLUSTER_MIN_MEMBERS)
      .map(([subnet, members]) => {
        let x = 0, y = 0, z = 0;
        for (const i of members) {
          const p = this.topology.nodes[i].position;
          x += p.x; y += p.y; z += p.z;
        }
        const n = members.length;
        // Label the district by the department that owns it rather than by its
        // CIDR: "finance" is what an operator is told on the phone.
        const tally = new Map();
        for (const i of members) {
          for (const tag of this.topology.nodes[i].tags || []) {
            if (["infrastructure", "access", "distribution", "core", "wireless"].includes(tag)) continue;
            tally.set(tag, (tally.get(tag) || 0) + 1);
          }
        }
        const dominant = [...tally.entries()].sort((a, b) => b[1] - a[1])[0];
        return {
          subnet, members,
          label: dominant ? dominant[0] : subnet,
          sublabel: subnet,
          center: { x: x / n, y: y / n, z: z / n },
          radius: Math.max(2.2, Math.cbrt(n) * 1.6),
        };
      });
  }

  /**
   * District labels: canvas-textured sprites, rescaled every frame so they keep
   * a constant apparent size instead of ballooning as the camera approaches.
   */
  _buildLabels() {
    for (const sprite of this.labelSprites) {
      this.scene.remove(sprite);
      sprite.material.map?.dispose();
      sprite.material.dispose();
    }
    this.labelSprites = [];

    for (const cluster of this.clusters) {
      const canvas = document.createElement("canvas");
      canvas.width = 640;
      canvas.height = 160;
      const ctx = canvas.getContext("2d");

      ctx.font = "600 52px 'Inter Variable', Inter, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = this.palette.text;
      ctx.shadowColor = "rgba(0,0,0,0.9)";
      ctx.shadowBlur = 12;
      ctx.fillText(cluster.label.toUpperCase(), 320, 58);

      ctx.font = "400 34px 'JetBrains Mono', ui-monospace, monospace";
      ctx.fillStyle = this.palette.textFaint;
      ctx.fillText(cluster.sublabel, 320, 112);

      const texture = new THREE.CanvasTexture(canvas);
      texture.colorSpace = THREE.SRGBColorSpace;
      texture.minFilter = THREE.LinearFilter;
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: texture, transparent: true, depthWrite: false, depthTest: false, opacity: 0.85,
      }));
      sprite.userData.aspect = 0.25;
      sprite.scale.set(1, 0.25, 1);
      sprite.position.set(
        cluster.center.x,
        cluster.center.y + this.topology.boundsRadius * 0.085,
        cluster.center.z
      );
      sprite.renderOrder = 7;
      this.scene.add(sprite);
      this.labelSprites.push(sprite);
    }
  }

  _allocateMeshes() {
    for (const mesh of [this.nodeMesh, this.haloMesh, this.linkLines, this.clusterMesh]) {
      if (mesh) {
        this.scene.remove(mesh);
        mesh.geometry?.dispose();
        mesh.material?.dispose();
      }
    }
    const count = this.topology.nodes.length;

    // --- nodes: shaded geometry with per-instance emissive ---------------
    const nodeGeometry = new THREE.IcosahedronGeometry(NODE_BASE_RADIUS, 2);
    this.emissiveAttribute = new THREE.InstancedBufferAttribute(new Float32Array(count), 1);
    this.emissiveAttribute.setUsage(THREE.DynamicDrawUsage);
    nodeGeometry.setAttribute("aEmissive", this.emissiveAttribute);

    const nodeMaterial = new THREE.MeshStandardMaterial({
      metalness: 0.15, roughness: 0.45, emissive: new THREE.Color(0xffffff),
      emissiveIntensity: 1,
    });
    // One instanced draw call cannot vary emissive intensity per instance, so
    // the attribute is threaded through the standard shader by hand. `vColor`
    // is already in scope from the instance colour, which tints the glow to
    // match the node instead of washing it out to white.
    nodeMaterial.onBeforeCompile = (shader) => {
      shader.vertexShader = "attribute float aEmissive;\nvarying float vEmissiveStrength;\n"
        + shader.vertexShader.replace(
          "#include <begin_vertex>",
          "#include <begin_vertex>\n  vEmissiveStrength = aEmissive;"
        );
      shader.fragmentShader = "varying float vEmissiveStrength;\n"
        + shader.fragmentShader.replace(
          "#include <emissivemap_fragment>",
          "#include <emissivemap_fragment>\n  totalEmissiveRadiance *= vEmissiveStrength * vColor;"
        );
    };

    this.nodeMesh = new THREE.InstancedMesh(nodeGeometry, nodeMaterial, count);
    this.nodeMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.nodeMesh.frustumCulled = false;
    this.scene.add(this.nodeMesh);

    // --- halos -----------------------------------------------------------
    const haloGeometry = new THREE.RingGeometry(0.74, 0.94, 32);
    const haloMaterial = new THREE.MeshBasicMaterial({
      side: THREE.DoubleSide, transparent: true, opacity: 0.9,
      depthWrite: false, blending: THREE.AdditiveBlending,
    });
    this.haloCapacity = Math.max(64, Math.min(count, 2048));
    this.haloMesh = new THREE.InstancedMesh(haloGeometry, haloMaterial, this.haloCapacity);
    this.haloMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.haloMesh.frustumCulled = false;
    this.haloMesh.renderOrder = 3;
    this.scene.add(this.haloMesh);

    // --- LOD cluster bubbles ---------------------------------------------
    const clusterGeometry = new THREE.IcosahedronGeometry(1, 3);
    const clusterMaterial = new THREE.MeshStandardMaterial({
      transparent: true, opacity: 0.22, depthWrite: false,
      metalness: 0.1, roughness: 0.6, emissive: new THREE.Color(0x111827),
    });
    this.clusterMesh = new THREE.InstancedMesh(
      clusterGeometry, clusterMaterial, Math.max(this.clusters.length, 1)
    );
    this.clusterMesh.frustumCulled = false;
    this.scene.add(this.clusterMesh);

    // --- links -----------------------------------------------------------
    const linkGeometry = new THREE.BufferGeometry();
    linkGeometry.setAttribute("position",
      new THREE.BufferAttribute(new Float32Array(this.topology.links.length * 6), 3));
    linkGeometry.setAttribute("color",
      new THREE.BufferAttribute(new Float32Array(this.topology.links.length * 6), 3));
    this.linkLines = new THREE.LineSegments(linkGeometry, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.34, blending: THREE.AdditiveBlending,
      depthWrite: false,
    }));
    this.linkLines.frustumCulled = false;
    this.scene.add(this.linkLines);
  }

  // -- LOD -------------------------------------------------------------
  _shouldCluster() {
    if (!this.lodEnabled || !this.clusters.length) return false;
    const distance = this.camera.position.distanceTo(this.controls.target);
    const nodeCount = this.topology.nodes.length;
    // Measured against the framing distance, not the bounds radius. Anything
    // absolute collapses a large network the moment it loads and never fires on
    // a small one; anything keyed to the radius alone ignores the fact that the
    // default view is itself chosen by the frustum fit.
    const overview = this.defaultDistance || Math.max(this.topology.boundsRadius, 1) * 2.2;
    return distance > overview * 1.3 || (nodeCount > 1500 && distance > overview * 0.75);
  }

  _passesFilter(node, state) {
    const f = this.filter;
    if (f.subnets?.size && !f.subnets.has(node.subnet)) return false;
    if (f.vendors?.size && !f.vendors.has(node.vendor)) return false;
    if (f.deviceTypes?.size && !f.deviceTypes.has(node.device_type)) return false;
    if (f.minSeverity) {
      const rank = state ? SEVERITY_RANK_LOOKUP[state.severity] : 0;
      if (rank < SEVERITY_RANK_LOOKUP[f.minSeverity]) return false;
    }
    if (f.query) {
      const q = f.query.toLowerCase();
      const haystack = `${node.name} ${node.ip} ${node.mac} ${node.vendor} ${node.subnet}`.toLowerCase();
      if (!haystack.includes(q)) return false;
    }
    return true;
  }

  /** Ask for a rebuild on the next frame; a burst of alerts costs one rebuild. */
  requestRebuild() {
    this._needsRebuild = true;
  }

  rebuild() {
    if (!this.topology || !this.nodeMesh) return;
    const matrix = new THREE.Matrix4();
    const color = new THREE.Color();
    const scaleVec = new THREE.Vector3();
    const position = new THREE.Vector3();
    const quaternion = new THREE.Quaternion();

    this.lodActive = this._shouldCluster();
    this.instanceToNode = [];
    this.pulsing = [];
    let slot = 0;

    // Pop-out only works when the target is unique. With a noisy sensor dozens
    // of nodes can be marked at once, and if every one pulses then none stands
    // out — the effect the design depends on is destroyed by its own success.
    // Halo, pulse and full glow are reserved for the most severe tier present
    // and capped; everything else stays coloured but static. Nothing is hidden,
    // only the animation budget is rationed.
    let topRank = 0;
    for (const state of this.states.values()) {
      topRank = Math.max(topRank, SEVERITY_RANK_LOOKUP[state.severity] || 0);
    }
    const animatedTier = new Set();
    if (topRank > 0) {
      [...this.states.values()]
        .filter((s) => (SEVERITY_RANK_LOOKUP[s.severity] || 0) === topRank)
        .sort((a, b) => (b.last_alert_at || 0) - (a.last_alert_at || 0))
        .slice(0, MAX_ANIMATED_NODES)
        .forEach((s) => animatedTier.add(s.node_id));
    }
    this.stats.animated = animatedTier.size;
    this.stats.suppressed = Math.max(this.states.size - animatedTier.size, 0);

    const clusteredAway = new Set();
    if (this.lodActive) {
      for (const cluster of this.clusters) for (const i of cluster.members) clusteredAway.add(i);
    }
    const anyAlerting = this.states.size > 0;

    this.topology.nodes.forEach((node, index) => {
      const state = this.states.get(node.id);
      const alerting = state && isAlerting(state.severity);
      if (!alerting) {
        if (this.lodActive && clusteredAway.has(index)) return;
        if (!this._passesFilter(node, state)) return;
      }
      if (slot >= this.nodeMesh.count) return;

      const style = alerting ? SEVERITY_STYLE[state.severity] : SEVERITY_STYLE.none;
      const selected = node.id === this.selectedId;
      const hovered = node.id === this.hoveredId;
      const scale = style.scale * (selected ? 1.5 : hovered ? 1.2 : 1);
      position.set(node.position.x, node.position.y, node.position.z);
      scaleVec.setScalar(scale);
      matrix.compose(position, quaternion, scaleVec);
      this.nodeMesh.setMatrixAt(slot, matrix);

      let emissive;
      if (alerting) {
        color.set(this.palette.severity[state.severity]);
        emissive = animatedTier.has(node.id) ? style.emissive : style.emissive * 0.4;
      } else if (selected) {
        color.set(this.palette.accent);
        emissive = 1.1;
      } else {
        color.set(this.palette.deviceType[node.device_type] || this.palette.deviceType.unknown);
        // Dim the quiet majority so the alerting few carry the eye — but not so
        // far that the network becomes unreadable: the operator still has to see
        // which district the hot node is in.
        color.multiplyScalar(anyAlerting ? 0.62 : 0.95);
        emissive = hovered ? 0.65 : anyAlerting ? 0.16 : 0.26;
      }
      this.nodeMesh.setColorAt(slot, color);
      this.emissiveAttribute.setX(slot, emissive);

      this.instanceToNode[slot] = index;
      if (alerting && style.pulse > 0 && animatedTier.has(node.id)) {
        this.pulsing.push({ slot, index, style, severity: state.severity, baseScale: scale });
      }
      slot += 1;
    });

    this.nodeMesh.count = slot;
    this.nodeMesh.instanceMatrix.needsUpdate = true;
    if (this.nodeMesh.instanceColor) this.nodeMesh.instanceColor.needsUpdate = true;
    this.emissiveAttribute.needsUpdate = true;
    this.stats.nodesRendered = slot;

    this._rebuildHalos();
    this._rebuildClusters();
    this._rebuildLinks();
    this._updateReticle();
  }

  _rebuildHalos() {
    const matrix = new THREE.Matrix4();
    const color = new THREE.Color();
    let slot = 0;
    for (const entry of this.pulsing) {
      if (slot >= this.haloCapacity) break;
      const node = this.topology.nodes[entry.index];
      matrix.makeScale(entry.style.halo, entry.style.halo, entry.style.halo);
      matrix.setPosition(node.position.x, node.position.y, node.position.z);
      this.haloMesh.setMatrixAt(slot, matrix);
      color.set(this.palette.severity[entry.severity]);
      this.haloMesh.setColorAt(slot, color);
      entry.haloSlot = slot;
      slot += 1;
    }
    this.haloMesh.count = slot;
    this.haloMesh.instanceMatrix.needsUpdate = true;
    if (this.haloMesh.instanceColor) this.haloMesh.instanceColor.needsUpdate = true;
  }

  _rebuildClusters() {
    if (!this.clusterMesh) return;
    if (!this.lodActive) {
      this.clusterMesh.count = 0;
      this.stats.clusters = 0;
      return;
    }
    const matrix = new THREE.Matrix4();
    const color = new THREE.Color();
    let slot = 0;
    for (const cluster of this.clusters) {
      // A collapsed district takes the colour of its worst member, so it still
      // signals that something inside it is wrong.
      let worst = "none";
      for (const i of cluster.members) {
        const state = this.states.get(this.topology.nodes[i].id);
        if (state && SEVERITY_RANK_LOOKUP[state.severity] > SEVERITY_RANK_LOOKUP[worst]) {
          worst = state.severity;
        }
      }
      matrix.makeScale(cluster.radius, cluster.radius * 0.72, cluster.radius);
      matrix.setPosition(cluster.center.x, cluster.center.y, cluster.center.z);
      this.clusterMesh.setMatrixAt(slot, matrix);
      color.set(worst === "none" ? this.palette.deviceType.unknown : this.palette.severity[worst]);
      this.clusterMesh.setColorAt(slot, color);
      slot += 1;
    }
    this.clusterMesh.count = slot;
    this.clusterMesh.instanceMatrix.needsUpdate = true;
    if (this.clusterMesh.instanceColor) this.clusterMesh.instanceColor.needsUpdate = true;
    this.stats.clusters = slot;
  }

  _rebuildLinks() {
    const { links, nodes } = this.topology;
    const positions = this.linkLines.geometry.attributes.position.array;
    const colors = this.linkLines.geometry.attributes.color.array;
    const visible = new Set(this.instanceToNode);
    const base = new THREE.Color(this.palette.link);
    const dim = new THREE.Color(this.palette.link).multiplyScalar(0.5);
    const active = new THREE.Color(this.palette.linkActive);
    const anyAlerting = this.states.size > 0;
    const flowColor = new THREE.Color(this.palette.linkFlow);
    const segments = [];
    let vertex = 0;

    for (const link of links) {
      const a = nodes[link.sourceIndex];
      const b = nodes[link.targetIndex];
      if (!a || !b) continue;
      if (this.lodActive) continue;             // links are noise at district zoom
      if (!visible.has(link.sourceIndex) && !visible.has(link.targetIndex)) continue;

      const touchesAlert = this.states.has(a.id) || this.states.has(b.id);
      const touchesSelection = a.id === this.selectedId || b.id === this.selectedId;
      // Quiet links are pushed well down so the lit ones read as the exception.
      const color = touchesAlert || touchesSelection
        ? active
        : (anyAlerting ? dim : base);

      for (const node of [a, b]) {
        positions[vertex * 3] = node.position.x;
        positions[vertex * 3 + 1] = node.position.y;
        positions[vertex * 3 + 2] = node.position.z;
        colors[vertex * 3] = color.r;
        colors[vertex * 3 + 1] = color.g;
        colors[vertex * 3 + 2] = color.b;
        vertex += 1;
      }

      // Only links touching a live incident carry visible traffic: flow
      // everywhere would be an aquarium, not a signal.
      if (touchesAlert && segments.length < 70) {
        segments.push({ from: a.position, to: b.position, color: flowColor });
      }
    }
    this.linkLines.geometry.setDrawRange(0, vertex);
    this.linkLines.geometry.attributes.position.needsUpdate = true;
    this.linkLines.geometry.attributes.color.needsUpdate = true;
    this.traffic.setSegments(
      this.effectsEnabled ? segments : [],
      Math.max(this.topology.boundsRadius * 0.008, 0.5)
    );
  }

  _updateReticle() {
    const index = this.selectedId ? this.topology?.index.get(this.selectedId) : undefined;
    if (index === undefined) {
      this.reticle.visible = false;
      return;
    }
    const node = this.topology.nodes[index];
    const state = this.states.get(node.id);
    const scale = (state ? SEVERITY_STYLE[state.severity].scale : 1) * 1.9;
    this.reticle.position.set(node.position.x, node.position.y, node.position.z);
    this.reticle.scale.setScalar(scale);
    this.reticle.visible = true;
  }

  // -- interaction -----------------------------------------------------
  pick() {
    if (!this.nodeMesh || !this.nodeMesh.count) return null;
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObject(this.nodeMesh, false);
    if (!hits.length) return null;
    const index = this.instanceToNode[hits[0].instanceId];
    const node = this.topology.nodes[index];
    return node ? { id: node.id, index, node } : null;
  }

  /**
   * Fly the camera to a node, stopping at a fraction of the scene radius rather
   * than a fixed number of world units. Flying all the way in fills the screen
   * with one sphere and throws away the context that makes this a map.
   */
  focusNode(nodeId, { distance = null, durationMs = 620 } = {}) {
    const index = this.topology?.index.get(nodeId);
    if (index === undefined) return null;
    const node = this.topology.nodes[index];
    distance = distance ?? Math.max(this.topology.boundsRadius * 0.4, 12);
    const target = new THREE.Vector3(node.position.x, node.position.y, node.position.z);
    const offset = new THREE.Vector3(distance * 0.6, distance * 0.72, distance * 0.6);
    this._flight = {
      startTarget: this.controls.target.clone(),
      endTarget: target,
      startPos: this.camera.position.clone(),
      endPos: target.clone().add(offset),
      start: performance.now(),
      duration: durationMs,
    };
    return node;
  }

  /**
   * Frame the whole network so it fills the panel at any window size.
   *
   * Not a hand-tuned multiple of the bounds radius. This layout is wide and
   * flat — the districts spread across the ground plane while the tiers are
   * only a few units tall — so a single radius badly over-estimates the
   * vertical space needed and leaves a third of the panel empty. Instead the
   * eight corners of the bounding box are transformed into camera space and the
   * distance is solved directly against both frustum planes, which is exact for
   * any aspect ratio and any shape of network.
   */
  frameAll() {
    const b = this.topology?.bounds;
    if (!b) return;

    // Aim at the centre of *mass*, not the centre of the bounding box. The box
    // is dominated by a couple of dozen routers sitting high above the floor,
    // so its centre is ~18 units above where 400 of the 428 nodes actually are —
    // which parks all the content in the bottom third of the panel.
    const target = new THREE.Vector3();
    for (const node of this.topology.nodes) {
      target.x += node.position.x; target.y += node.position.y; target.z += node.position.z;
    }
    target.divideScalar(Math.max(this.topology.nodes.length, 1));
    // A raised three-quarter view: the tiers read as tiers, and the districts
    // stay separated instead of collapsing into one mass.
    const dir = new THREE.Vector3(0.5, 0.56, 0.86).normalize();

    // Camera-space basis for that direction.
    const forward = dir.clone().negate();                       // camera looks along -dir
    const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), forward).normalize();
    const up = new THREE.Vector3().crossVectors(forward, right).normalize();

    const vFov = (this.camera.fov * Math.PI) / 180;
    const tanV = Math.tan(vFov / 2);
    const tanH = tanV * Math.max(this.camera.aspect, 0.1);

    // Fit to the real node positions, not to the corners of the bounding box.
    // This layout is a ring of districts — roughly a disc — and a disc
    // inscribed in its own AABB leaves the four corners empty, so fitting the
    // box shrinks the content to about three quarters of the frame for nothing.
    let distance = 0;
    const offset = new THREE.Vector3();
    for (const node of this.topology.nodes) {
      offset.set(node.position.x, node.position.y, node.position.z).sub(target);
      const cx = offset.dot(right);
      const cy = offset.dot(up);
      const depth = offset.dot(dir);   // how far this node already sits toward the camera
      distance = Math.max(distance, Math.abs(cx) / tanH + depth, Math.abs(cy) / tanV + depth);
    }
    distance = Math.max(distance * 1.05, this.topology.boundsRadius * 0.4);
    // The default overview distance is the reference the LOD threshold is
    // measured against — "zoomed out past the overview", not an absolute
    // number of world units that depends on how big the network happens to be.
    this.defaultDistance = distance;

    this.controls.target.copy(target);
    this.camera.position.copy(target).add(dir.multiplyScalar(distance));
    this.controls.update();
  }

  // -- frame loop ------------------------------------------------------
  _animate() {
    if (!this.running) return;
    requestAnimationFrame(this._animate);
    const dt = Math.min(this.clock.getDelta(), 0.1);
    const now = performance.now();
    this.frame += 1;
    this.renderer.info.reset();

    if (this._flight) {
      const t = Math.min((now - this._flight.start) / this._flight.duration, 1);
      // ease-in-out cubic: settles without the abrupt stop of a linear lerp
      const ease = t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2;
      this.camera.position.lerpVectors(this._flight.startPos, this._flight.endPos, ease);
      this.controls.target.lerpVectors(this._flight.startTarget, this._flight.endTarget, ease);
      if (t >= 1) this._flight = null;
    }

    this.controls.update();

    if (this._needsRebuild) {
      this._needsRebuild = false;
      this.rebuild();
    }

    this._pulse(now);
    this._updateLabels();
    if (this.effectsEnabled) {
      this.traffic.update(dt);
      this.shockwaves.update(dt, this.camera.quaternion);
      if (this.reticle.visible) {
        this.reticle.quaternion.copy(this.camera.quaternion);
        this.reticle.userData.ring.rotation.z += dt * 0.9;
      }
    }

    const shouldCluster = this._shouldCluster();
    if (shouldCluster !== this.lodActive) this.requestRebuild();

    if (this._pickPending) {
      this._pickPending = false;
      const hit = this.pick();
      const id = hit ? hit.id : null;
      if (id !== this.hoveredId) {
        this.hoveredId = id;
        this.canvas.style.cursor = id ? "pointer" : "grab";
        this.onHover(hit);
        this.requestRebuild();
      }
    }

    if (this.effectsEnabled && this.composer) this.composer.render(dt);
    else this.renderer.render(this.scene, this.camera);

    this._trackFps(dt);
  }

  _pulse(now) {
    if (!this.pulsing?.length) return;
    const matrix = new THREE.Matrix4();
    const position = new THREE.Vector3();
    const quaternion = new THREE.Quaternion();
    const scaleVec = new THREE.Vector3();
    const seconds = now / 1000;

    for (const entry of this.pulsing) {
      const node = this.topology.nodes[entry.index];
      const phase = Math.sin(seconds * entry.style.pulse * Math.PI * 2);
      const amplitude = 0.18 + 0.06 * entry.style.pulse;
      const scale = entry.baseScale * (1 + phase * amplitude);
      position.set(node.position.x, node.position.y, node.position.z);
      scaleVec.setScalar(scale);
      matrix.compose(position, quaternion, scaleVec);
      this.nodeMesh.setMatrixAt(entry.slot, matrix);
      // Glow breathes with the pulse, so the cue survives being seen in
      // peripheral vision where size changes are hard to judge.
      this.emissiveAttribute.setX(entry.slot, entry.style.emissive * (1 + phase * 0.45));

      if (entry.haloSlot !== undefined) {
        const haloScale = entry.style.halo * (1 + (phase * 0.5 + 0.5) * 0.4);
        matrix.makeScale(haloScale, haloScale, haloScale);
        matrix.multiply(new THREE.Matrix4().makeRotationFromQuaternion(this.camera.quaternion));
        matrix.setPosition(node.position.x, node.position.y, node.position.z);
        this.haloMesh.setMatrixAt(entry.haloSlot, matrix);
      }
    }
    this.nodeMesh.instanceMatrix.needsUpdate = true;
    this.emissiveAttribute.needsUpdate = true;
    this.haloMesh.instanceMatrix.needsUpdate = true;
  }

  _updateLabels() {
    if (!this.labelSprites?.length) return;
    const radius = this.topology.boundsRadius;
    const camDistance = this.camera.position.distanceTo(this.controls.target);
    for (const sprite of this.labelSprites) {
      const distance = this.camera.position.distanceTo(sprite.position);
      const scale = distance * 0.105;
      sprite.scale.set(scale, scale * sprite.userData.aspect, 1);
      const nearness = camDistance / (radius * 0.6);
      sprite.material.opacity = Math.max(0, Math.min(0.9, (nearness - 0.3) * 1.2));
    }
  }

  _trackFps(dt) {
    this.lastFrameTimes.push(dt);
    if (this.lastFrameTimes.length > 60) this.lastFrameTimes.shift();
    if (this.frame % 15 !== 0) return;

    const mean = this.lastFrameTimes.reduce((a, b) => a + b, 0) / this.lastFrameTimes.length;
    this.stats.fps = mean > 0 ? 1 / mean : 0;
    this.stats.drawCalls = this.renderer.info.render.calls;
    this.stats.triangles = this.renderer.info.render.triangles;
    this.stats.memory = this.renderer.info.memory;

    // Adaptive quality: a map that stutters is worse than a map that is plain,
    // so sustained low frame rates drop the effects rather than the frame rate.
    if (this.qualityMode !== "auto" || this.lastFrameTimes.length < 60) return;
    if (this.effectsEnabled && this.stats.fps < 28) {
      this._slowFrames = (this._slowFrames || 0) + 1;
      if (this._slowFrames > 6) {
        this.setEffects(false);
        this.stats.quality = "auto-reduced";
      }
    } else {
      this._slowFrames = 0;
    }
  }

  dispose() {
    this.running = false;
    window.removeEventListener("resize", this._onResize);
    this.canvas.removeEventListener("pointermove", this._onPointerMove);
    this.canvas.removeEventListener("click", this._onClick);
    for (const sprite of this.labelSprites) {
      sprite.material.map?.dispose();
      sprite.material.dispose();
    }
    this.traffic.dispose();
    this.shockwaves.dispose();
    this.controls.dispose();
    this.composer?.dispose?.();
    this.renderer.dispose();
  }
}
