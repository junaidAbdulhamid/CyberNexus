/**
 * The 3D map.
 *
 * Performance model, in one line: **one draw call for all nodes, one for all
 * halos, one for all links.** Everything else follows from that.
 *
 *  - Nodes are a single `InstancedMesh`. 5,000 hosts cost the GPU the same as
 *    one, and changing a node's colour is a buffer write rather than a scene
 *    graph mutation.
 *  - Links are a single `LineSegments` with per-vertex colour.
 *  - Instance data is rebuilt only when something structural changes (topology,
 *    filter, alert state, LOD level) - never per frame. The per-frame work is
 *    limited to pulsing the handful of nodes that are actually alerting.
 *  - Level of detail collapses each subnet into one cluster sphere when the
 *    camera is far enough away that individual hosts are sub-pixel anyway.
 *
 * The rule that overrides all of the above: **an alerting node is always drawn
 * individually**, at full size, whatever the LOD level or the active filter. A
 * visualisation that can hide the thing you are looking for is worse than a
 * list, and the entire point of this view is that the alert finds the operator
 * rather than the other way round.
 */
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { DEVICE_GLYPH, SEVERITY_STYLE, getPalette, hexToInt, isAlerting } from "../lib/theme.js";

const NODE_BASE_RADIUS = 0.42;
const CLUSTER_MIN_MEMBERS = 8;
//: Most nodes that may pulse simultaneously. Above a handful, motion stops
//: being a pop-out cue and becomes visual noise.
const MAX_ANIMATED_NODES = 6;

export class NetworkScene {
  constructor(canvas, { paletteName = "default", onSelect = null, onHover = null } = {}) {
    this.canvas = canvas;
    this.palette = getPalette(paletteName);
    this.onSelect = onSelect || (() => {});
    this.onHover = onHover || (() => {});

    this.topology = null;
    this.states = new Map();        // nodeId -> {severity, score, ...}
    this.filter = { subnets: null, vendors: null, deviceTypes: null, minSeverity: null, query: "" };
    this.visibleIndices = [];       // topology index -> instance slot
    this.instanceToNode = [];       // instance slot -> topology index
    this.selectedId = null;
    this.hoveredId = null;
    this.lodEnabled = true;
    this.lodActive = false;
    this.clusters = [];
    this.frame = 0;
    this.lastFrameTimes = [];
    this.stats = { fps: 0, drawCalls: 0, nodesRendered: 0, clusters: 0, triangles: 0 };

    this._initRenderer();
    this._initScene();
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
      canvas: this.canvas,
      antialias: true,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(this.canvas.clientWidth, this.canvas.clientHeight, false);
    this.renderer.setClearColor(hexToInt(this.palette.background), 1);
  }

  _initScene() {
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.Fog(hexToInt(this.palette.fog), 120, 400);

    const aspect = this.canvas.clientWidth / Math.max(this.canvas.clientHeight, 1);
    this.camera = new THREE.PerspectiveCamera(55, aspect, 0.5, 2000);
    this.camera.position.set(70, 62, 70);

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.maxDistance = 600;
    this.controls.minDistance = 4;

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.75));
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(50, 120, 60);
    this.scene.add(key);
    const rim = new THREE.DirectionalLight(0x88aaff, 0.35);
    rim.position.set(-60, -20, -40);
    this.scene.add(rim);

    this.groundGrid = new THREE.GridHelper(400, 40, 0x1b2230, 0x141a25);
    this.groundGrid.position.y = -6;
    this.scene.add(this.groundGrid);

    // Instanced meshes are allocated on first topology load, when the count is
    // known; three needs a fixed capacity per InstancedMesh.
    this.nodeMesh = null;
    this.haloMesh = null;
    this.linkLines = null;
    this.clusterMesh = null;
  }

  /** Fog, grid and camera clipping follow the size of the loaded network. */
  _fitSceneScale() {
    const radius = this.topology.boundsRadius;
    this.scene.fog.near = radius * 1.6;
    this.scene.fog.far = radius * 5.0;
    this.camera.far = radius * 12;
    this.camera.near = Math.max(radius / 400, 0.1);
    this.camera.updateProjectionMatrix();
    this.controls.maxDistance = radius * 6;
    this.controls.minDistance = Math.max(radius / 60, 1.5);

    this.scene.remove(this.groundGrid);
    this.groundGrid.geometry.dispose();
    const size = radius * 3;
    this.groundGrid = new THREE.GridHelper(size, 40, 0x1b2230, 0x141a25);
    const b = this.topology.bounds;
    this.groundGrid.position.set(b?.center.x || 0, (b?.min.y ?? 0) - radius * 0.12, b?.center.z || 0);
    this.scene.add(this.groundGrid);
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
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  setPalette(paletteName) {
    this.palette = getPalette(paletteName);
    this.renderer.setClearColor(hexToInt(this.palette.background), 1);
    this.scene.fog.color.setHex(hexToInt(this.palette.fog));
    if (this.topology) this._buildLabels();
    this.rebuild();
  }

  // -- data ------------------------------------------------------------
  setTopology(topology) {
    this.topology = topology;
    // Cache the scene scale; LOD thresholds, fog and the grid all key off it.
    this.topology.boundsRadius = Math.max(topology.bounds?.radius || 40, 10);
    this._fitSceneScale();
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
    if (!state || state.severity === "none") this.states.delete(nodeId);
    else this.states.set(nodeId, state);
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

  /**
   * Text labels over each district.
   *
   * Nine canvas-textured sprites, so the cost is negligible - and they are what
   * turn "a red dot somewhere on the right" into "a red dot in the datacenter".
   * Orientation is billboarded by Sprite itself, so they stay readable from any
   * camera angle.
   */
  _buildLabels() {
    for (const sprite of this.labelSprites || []) {
      this.scene.remove(sprite);
      sprite.material.map?.dispose();
      sprite.material.dispose();
    }
    this.labelSprites = [];
    const radius = this.topology.boundsRadius;

    for (const cluster of this.clusters) {
      const text = cluster.label || cluster.subnet;
      const canvas = document.createElement("canvas");
      canvas.width = 512;
      canvas.height = 128;
      const ctx = canvas.getContext("2d");
      ctx.font = "600 48px system-ui, -apple-system, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "rgba(11,14,20,0.55)";
      ctx.fillRect(0, 26, 512, 76);
      ctx.fillStyle = this.palette.textDim;
      ctx.fillText(text, 256, 64);

      const texture = new THREE.CanvasTexture(canvas);
      texture.minFilter = THREE.LinearFilter;
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
        map: texture, transparent: true, depthWrite: false, opacity: 0.8,
      }));
      // Base scale only; `_updateLabels` rescales every frame so the label
      // keeps a constant size on screen instead of ballooning as the camera
      // approaches. A world-sized label is unreadable at both ends of the zoom.
      sprite.userData.aspect = 0.25;
      sprite.scale.set(1, 0.25, 1);
      sprite.position.set(
        cluster.center.x,
        cluster.center.y + radius * 0.1,
        cluster.center.z
      );
      sprite.renderOrder = 3;
      this.scene.add(sprite);
      this.labelSprites.push(sprite);
    }
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
        // CIDR: "finance" is what an operator is told on the phone, and
        // "10.20.10.0/24" is what they then have to look up.
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
          label: dominant ? `${dominant[0]}  ${subnet}` : subnet,
          center: { x: x / n, y: y / n, z: z / n },
          radius: Math.max(2.2, Math.cbrt(n) * 1.6),
        };
      });
    this.clusterOf = new Map();
    this.clusters.forEach((cluster, ci) => {
      for (const i of cluster.members) this.clusterOf.set(i, ci);
    });
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

    const nodeGeometry = new THREE.IcosahedronGeometry(NODE_BASE_RADIUS, 1);
    const nodeMaterial = new THREE.MeshLambertMaterial({ vertexColors: false });
    this.nodeMesh = new THREE.InstancedMesh(nodeGeometry, nodeMaterial, count);
    this.nodeMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.nodeMesh.frustumCulled = false;
    this.scene.add(this.nodeMesh);

    // Halos: rings around alerting nodes. Capacity is deliberately generous -
    // during a worm outbreak, "a lot of nodes are alerting" is the normal case.
    const haloGeometry = new THREE.RingGeometry(0.72, 0.92, 24);
    const haloMaterial = new THREE.MeshBasicMaterial({
      side: THREE.DoubleSide, transparent: true, opacity: 0.85, depthWrite: false,
    });
    this.haloCapacity = Math.max(64, Math.min(count, 2048));
    this.haloMesh = new THREE.InstancedMesh(haloGeometry, haloMaterial, this.haloCapacity);
    this.haloMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    this.haloMesh.frustumCulled = false;
    this.haloMesh.renderOrder = 2;
    this.scene.add(this.haloMesh);

    const clusterGeometry = new THREE.IcosahedronGeometry(1, 2);
    const clusterMaterial = new THREE.MeshLambertMaterial({
      transparent: true, opacity: 0.35, depthWrite: false,
    });
    this.clusterMesh = new THREE.InstancedMesh(
      clusterGeometry, clusterMaterial, Math.max(this.clusters.length, 1)
    );
    this.clusterMesh.frustumCulled = false;
    this.scene.add(this.clusterMesh);

    const linkGeometry = new THREE.BufferGeometry();
    const positions = new Float32Array(this.topology.links.length * 6);
    const colors = new Float32Array(this.topology.links.length * 6);
    linkGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    linkGeometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const linkMaterial = new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.55,
    });
    this.linkLines = new THREE.LineSegments(linkGeometry, linkMaterial);
    this.linkLines.frustumCulled = false;
    this.scene.add(this.linkLines);
  }

  // -- LOD -------------------------------------------------------------
  _shouldCluster() {
    if (!this.lodEnabled || !this.clusters.length) return false;
    const distance = this.camera.position.distanceTo(this.controls.target);
    const nodeCount = this.topology.nodes.length;
    // Thresholds are relative to the scene's own size, not absolute world
    // units: an absolute threshold collapses a large network the moment it
    // loads (its bounds are bigger) while never triggering on a small one.
    // `frameAll` parks the camera at ~2.1x the bounds radius, so 2.6x means
    // "the operator has zoomed out past the default overview".
    const radius = Math.max(this.topology.boundsRadius || 1, 1);
    const farLimit = radius * 2.6;
    const denseLimit = radius * 1.4;
    return distance > farLimit || (nodeCount > 1500 && distance > denseLimit);
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

  /**
   * Request a rebuild on the next frame.
   *
   * A burst of alerts from one incident arrives as dozens of separate updates
   * within a few milliseconds. Rebuilding every instance buffer for each one
   * made a 20-alert port scan cost twenty full rebuilds and pushed
   * alert-to-pixel latency into the hundreds of milliseconds. Coalescing to at
   * most one rebuild per frame makes a burst cost the same as a single alert.
   */
  requestRebuild() {
    this._needsRebuild = true;
  }

  /** Rebuild all instance buffers. Called on change, never per frame. */
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

    // Pop-out only works when the target is unique. With a noisy sensor, dozens
    // of low-severity nodes can be marked at once, and if every one of them
    // pulses then none of them stands out - the effect the whole design depends
    // on is destroyed by its own success. So the halo-and-pulse treatment is
    // reserved for the most severe tier currently present, and capped: everything
    // else stays coloured and enlarged, but static. Nothing is hidden; only the
    // *animation* budget is rationed.
    let topRank = 0;
    for (const state of this.states.values()) {
      topRank = Math.max(topRank, SEVERITY_RANK_LOOKUP[state.severity] || 0);
    }
    const animatedTier = new Set();
    if (topRank > 0) {
      const candidates = [...this.states.values()]
        .filter((s) => (SEVERITY_RANK_LOOKUP[s.severity] || 0) === topRank)
        .sort((a, b) => (b.last_alert_at || 0) - (a.last_alert_at || 0))
        .slice(0, MAX_ANIMATED_NODES);
      for (const state of candidates) animatedTier.add(state.node_id);
    }
    this.animatedCount = animatedTier.size;
    this.suppressedCount = Math.max(this.states.size - animatedTier.size, 0);

    const clusteredAway = new Set();
    if (this.lodActive) {
      for (const cluster of this.clusters) for (const i of cluster.members) clusteredAway.add(i);
    }

    this.topology.nodes.forEach((node, index) => {
      const state = this.states.get(node.id);
      const alerting = state && isAlerting(state.severity);
      // Alerting nodes ignore both LOD collapse and the active filter: the map
      // must never hide the thing the operator is looking for.
      if (!alerting) {
        if (this.lodActive && clusteredAway.has(index)) return;
        if (!this._passesFilter(node, state)) return;
      }
      if (slot >= this.nodeMesh.count) return;

      const style = alerting ? SEVERITY_STYLE[state.severity] : SEVERITY_STYLE.none;
      const selected = node.id === this.selectedId;
      const scale = style.scale * (selected ? 1.45 : 1);
      position.set(node.position.x, node.position.y, node.position.z);
      scaleVec.set(scale, scale, scale);
      matrix.compose(position, quaternion, scaleVec);
      this.nodeMesh.setMatrixAt(slot, matrix);

      if (alerting) {
        color.set(this.palette.severity[state.severity]);
      } else if (selected) {
        color.set(this.palette.accent);
      } else {
        color.set(this.palette.deviceType[node.device_type] || this.palette.deviceType.unknown);
        // Dim non-alerting nodes so the alerting ones carry the eye - the
        // single biggest contributor to how fast an incident is spotted. Not
        // so far that the network becomes unreadable: the operator still has
        // to see which district the hot node is in.
        color.multiplyScalar(this.states.size > 0 ? 0.55 : 0.9);
      }
      this.nodeMesh.setColorAt(slot, color);

      this.instanceToNode[slot] = index;
      if (alerting && style.pulse > 0 && animatedTier.has(node.id)) {
        this.pulsing.push({ slot, index, style, severity: state.severity, baseScale: scale });
      }
      slot += 1;
    });

    this.nodeMesh.count = slot;
    this.nodeMesh.instanceMatrix.needsUpdate = true;
    if (this.nodeMesh.instanceColor) this.nodeMesh.instanceColor.needsUpdate = true;
    this.stats.nodesRendered = slot;
    this.stats.animated = this.animatedCount;
    this.stats.suppressed = this.suppressedCount;

    this._rebuildHalos();
    this._rebuildClusters();
    this._rebuildLinks();
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
      // A cluster takes the colour of its most severe member, so a collapsed
      // district still signals that something inside it is wrong.
      let worst = "none";
      for (const i of cluster.members) {
        const state = this.states.get(this.topology.nodes[i].id);
        if (state && SEVERITY_RANK_LOOKUP[state.severity] > SEVERITY_RANK_LOOKUP[worst]) {
          worst = state.severity;
        }
      }
      matrix.makeScale(cluster.radius, cluster.radius * 0.7, cluster.radius);
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
    const active = new THREE.Color(this.palette.linkActive);
    let vertex = 0;

    for (const link of links) {
      const a = nodes[link.sourceIndex];
      const b = nodes[link.targetIndex];
      if (!a || !b) continue;
      if (this.lodActive) continue;   // links are noise at district zoom
      if (!visible.has(link.sourceIndex) && !visible.has(link.targetIndex)) continue;

      const touchesAlert = this.states.has(a.id) || this.states.has(b.id);
      const touchesSelection = a.id === this.selectedId || b.id === this.selectedId;
      const color = touchesAlert || touchesSelection ? active : base;

      positions[vertex * 3] = a.position.x;
      positions[vertex * 3 + 1] = a.position.y;
      positions[vertex * 3 + 2] = a.position.z;
      colors[vertex * 3] = color.r; colors[vertex * 3 + 1] = color.g; colors[vertex * 3 + 2] = color.b;
      vertex += 1;
      positions[vertex * 3] = b.position.x;
      positions[vertex * 3 + 1] = b.position.y;
      positions[vertex * 3 + 2] = b.position.z;
      colors[vertex * 3] = color.r; colors[vertex * 3 + 1] = color.g; colors[vertex * 3 + 2] = color.b;
      vertex += 1;
    }
    this.linkLines.geometry.setDrawRange(0, vertex);
    this.linkLines.geometry.attributes.position.needsUpdate = true;
    this.linkLines.geometry.attributes.color.needsUpdate = true;
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
   * Fly the camera to a node.
   *
   * The stopping distance is a fraction of the scene radius rather than a fixed
   * number of world units. Flying all the way in fills the screen with one
   * sphere and throws away the context that makes this a map at all - the
   * operator needs to see the host *and* the district it sits in.
   */
  focusNode(nodeId, { distance = null, durationMs = 550 } = {}) {
    const index = this.topology?.index.get(nodeId);
    if (index === undefined) return null;
    const node = this.topology.nodes[index];
    distance = distance ?? Math.max(this.topology.boundsRadius * 0.22, 8);
    const target = new THREE.Vector3(node.position.x, node.position.y, node.position.z);
    const offset = new THREE.Vector3(distance * 0.6, distance * 0.7, distance * 0.6);
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

  frameAll() {
    const b = this.topology?.bounds;
    if (!b) return;
    const radius = Math.max(b.radius, 10);
    this.controls.target.set(b.center.x, b.center.y, b.center.z);
    this.camera.position.set(
      b.center.x + radius * 1.25, b.center.y + radius * 1.1, b.center.z + radius * 1.25
    );
    this.controls.update();
  }

  // -- frame loop ------------------------------------------------------
  _animate() {
    if (!this.running) return;
    requestAnimationFrame(this._animate);
    const dt = this.clock.getDelta();
    const now = performance.now();
    this.frame += 1;

    if (this._flight) {
      const t = Math.min((now - this._flight.start) / this._flight.duration, 1);
      const ease = t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t;
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

    // LOD level is a function of camera distance, so it is re-evaluated as the
    // camera settles - but only rebuilt when the level actually flips.
    const shouldCluster = this._shouldCluster();
    if (shouldCluster !== this.lodActive) this.rebuild();

    if (this._pickPending) {
      this._pickPending = false;
      const hit = this.pick();
      const id = hit ? hit.id : null;
      if (id !== this.hoveredId) {
        this.hoveredId = id;
        this.canvas.style.cursor = id ? "pointer" : "default";
        this.onHover(hit);
      }
    }

    this.renderer.render(this.scene, this.camera);
    this._trackFps(dt);
  }

  /** Keep district labels at a constant apparent size, and fade them when the
   *  camera is close enough that individual hosts are the subject. */
  _updateLabels() {
    if (!this.labelSprites?.length) return;
    const radius = this.topology.boundsRadius;
    const camDistance = this.camera.position.distanceTo(this.controls.target);
    for (const sprite of this.labelSprites) {
      const distance = this.camera.position.distanceTo(sprite.position);
      // ~9% of the viewport height, whatever the distance.
      const scale = distance * 0.09;
      sprite.scale.set(scale, scale * sprite.userData.aspect, 1);
      const nearness = camDistance / (radius * 0.6);
      sprite.material.opacity = Math.max(0, Math.min(0.85, (nearness - 0.35) * 1.2));
    }
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
      scaleVec.set(scale, scale, scale);
      matrix.compose(position, quaternion, scaleVec);
      this.nodeMesh.setMatrixAt(entry.slot, matrix);

      if (entry.haloSlot !== undefined) {
        const haloScale = entry.style.halo * (1 + (phase * 0.5 + 0.5) * 0.35);
        matrix.makeScale(haloScale, haloScale, haloScale);
        // Billboard the ring so it reads as a halo from any camera angle.
        matrix.multiply(new THREE.Matrix4().makeRotationFromQuaternion(this.camera.quaternion));
        matrix.setPosition(node.position.x, node.position.y, node.position.z);
        this.haloMesh.setMatrixAt(entry.haloSlot, matrix);
      }
    }
    this.nodeMesh.instanceMatrix.needsUpdate = true;
    this.haloMesh.instanceMatrix.needsUpdate = true;
  }

  _trackFps(dt) {
    this.lastFrameTimes.push(dt);
    if (this.lastFrameTimes.length > 60) this.lastFrameTimes.shift();
    if (this.frame % 15 === 0) {
      const mean = this.lastFrameTimes.reduce((a, b) => a + b, 0) / this.lastFrameTimes.length;
      this.stats.fps = mean > 0 ? 1 / mean : 0;
      this.stats.drawCalls = this.renderer.info.render.calls;
      this.stats.triangles = this.renderer.info.render.triangles;
      this.stats.memory = this.renderer.info.memory;
    }
  }

  dispose() {
    this.running = false;
    for (const sprite of this.labelSprites || []) {
      sprite.material.map?.dispose();
      sprite.material.dispose();
    }
    window.removeEventListener("resize", this._onResize);
    this.canvas.removeEventListener("pointermove", this._onPointerMove);
    this.canvas.removeEventListener("click", this._onClick);
    this.controls.dispose();
    this.renderer.dispose();
  }
}

const SEVERITY_RANK_LOOKUP = { none: 0, info: 1, low: 2, medium: 3, high: 4, critical: 5 };
export { SEVERITY_RANK_LOOKUP };
