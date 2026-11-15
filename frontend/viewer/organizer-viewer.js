// Inside the Box: interactive 3D organizer viewer (shared by the web UI and preview/).
//
// Source of truth: layout.json. Each printable box is its own STL (the exact exported geometry, centred on
// x/y = 0 with its base at z = 0), placed by rotating it rotation_degrees about +z and translating it to
// center_mm, under a Z-up root, so stacked boxes keep their real height. Everything the user does here
// (select, hide, explode, contents, colours) is visual only: no printable geometry is changed.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { CSS2DRenderer, CSS2DObject } from 'three/addons/renderers/CSS2DRenderer.js';
import { contentsProxies } from './contents.js';

const BG = 0xece8e1;
const INK = 0x16100a;
const ACCENT = 0x3212ae;
const SET_COLORS = ['#d64d47', '#4578cc', '#edb840', '#8c6bbf', '#54a373', '#e68538', '#5aa6b5', '#c24f6e'];
export const FILAMENTS = [
  { id: 'set', label: 'Set colours', swatch: 'conic-gradient(#d64d47 0 25%, #4578cc 0 50%, #edb840 0 75%, #54a373 0)' },
  { id: 'white', label: 'PLA White', color: '#efebe2', roughness: 0.58 },
  { id: 'charcoal', label: 'Matte Charcoal', color: '#3b3c40', roughness: 0.78 },
  { id: 'sage', label: 'Sage Green', color: '#8fa688', roughness: 0.6 },
  { id: 'terracotta', label: 'Terracotta', color: '#c4694b', roughness: 0.6 },
  { id: 'gold', label: 'Silk Gold', color: '#c9a04b', roughness: 0.3, metalness: 0.4 },
];
const PIECE_COLORS = ['#2f3b4c', '#b9473f', '#2f7d68', '#e2aa3c', '#6a539b', '#8e939b', '#d98a5f', '#3e7cb1'];
const CARD_COLORS = ['#f3eee2', '#e6dcc6', '#dfe7ea', '#efe1d6'];
const VIEW_DIRS = { iso: [1, 0.82, 1.18], top: [0, 1, 0.0001], front: [0, 0.16, 1] };
const STYLE_LABEL = { loose: 'Loose pieces', stack: 'One stack', card_stack: 'Card deck', tight_pack: 'Packed grid' };
const STORE = 'itb.viewer.';

const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const mm = (v) => (Number.isFinite(+v) ? (+v).toFixed(1) : '–');
const store = {
  get(k, d) { try { const v = localStorage.getItem(STORE + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(STORE + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

const ICON = {
  close: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  play: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.5v9l7-4.5z" fill="currentColor"/></svg>',
  download: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.5v8M4.5 7.5L8 11l3.5-3.5M3 13.5h10" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

const TEMPLATE = `
<div class="itb-stage">
  <div class="itb-canvas"></div>
  <div class="itb-hover" hidden></div>
  <div class="itb-topbar">
    <div class="itb-title"><strong></strong><span></span></div>
    <div class="itb-top-actions">
      <button type="button" class="itb-chip itb-showall" hidden></button>
      <button type="button" class="itb-close" hidden aria-label="Close 3D studio">${ICON.close}<span>Close</span></button>
    </div>
  </div>
  <aside class="itb-panel" hidden aria-live="polite"></aside>
  <p class="itb-note" hidden></p>
  <div class="itb-menu" hidden role="menu" aria-label="Filament colour"></div>
  <div class="itb-toolbar" role="toolbar" aria-label="3D view controls">
    <div class="itb-seg" role="group" aria-label="Camera">
      <button type="button" data-view="iso" aria-pressed="true">Iso</button>
      <button type="button" data-view="top" aria-pressed="false">Top</button>
      <button type="button" data-view="front" aria-pressed="false">Front</button>
    </div>
    <button type="button" data-act="fit" title="Fit everything in view">Fit</button>
    <button type="button" data-act="reset" title="Reset camera, explode and visibility">Reset</button>
    <span class="itb-sep"></span>
    <label class="itb-explode" title="Separate the boxes"><span>Explode</span>
      <input type="range" min="0" max="1.5" step="0.01" value="0" aria-label="Explode distance"></label>
    <button type="button" data-act="assemble" title="Play the assembly order">${ICON.play}<span>Assemble</span></button>
    <span class="itb-sep"></span>
    <button type="button" data-toggle="contents" aria-pressed="false" title="Show approximate stand-ins for the stored pieces">Contents</button>
    <button type="button" data-toggle="box" aria-pressed="true" title="Show the game box outline">Game box</button>
    <button type="button" data-act="filament" aria-haspopup="menu" aria-expanded="false" title="Preview filament colours"><i class="itb-swatch"></i><span>Filament</span></button>
  </div>
</div>`;

function placementOf(t, i) {
  const o = t.outer_mm || {};
  const rot = Number(t.rotation_degrees) || 0;
  let c = null;
  if (t.center_mm) c = [t.center_mm.x, t.center_mm.y, t.center_mm.z];
  else if (Array.isArray(t.assembled_position_mm)) c = t.assembled_position_mm;
  else if (t.position_mm && t.position_mm.x != null) {
    const p = t.position_mm;
    const fp = t.footprint_in_box_mm
      || (rot % 180 ? { width: o.length, length: o.width } : { width: o.width, length: o.length });
    c = [p.x + (fp.width || 0) / 2, p.y + (fp.length || 0) / 2, p.z || 0];
  } else c = [(i % 3) * 70 + 35, Math.floor(i / 3) * 70 + 35, 0];       // no layout: a simple grid
  const e = Array.isArray(t.exploded_position_mm) ? t.exploded_position_mm
    : [c[0], c[1], c[2] + (o.height || 30) + 30 * ((t.layer || 0) + 1)];
  return { a: new THREE.Vector3(...c.map(Number)), e: new THREE.Vector3(...e.map(Number)), rot };
}

function unitGeometries() {
  const cyl = (n) => new THREE.CylinderGeometry(0.5, 0.5, 1, n).rotateX(Math.PI / 2);
  return {
    box: new THREE.BoxGeometry(1, 1, 1), card: new THREE.BoxGeometry(1, 1, 1), cylinder: cyl(28), hex: cyl(6),
    tri: cyl(3), sphere: new THREE.SphereGeometry(0.5, 20, 14),
    cone: new THREE.ConeGeometry(0.5, 1, 24).rotateX(Math.PI / 2),
  };
}

export class OrganizerViewer {
  constructor(host, opts = {}) {
    this.opts = { compact: false, onSelect: null, onClose: null, ...opts };
    this.el = document.createElement('div');
    this.el.className = 'itb-viewer';
    this.el.innerHTML = TEMPLATE;
    host.append(this.el);
    const q = (s) => this.el.querySelector(s);
    this.ui = {
      stage: q('.itb-stage'), canvas: q('.itb-canvas'), hover: q('.itb-hover'), title: q('.itb-title'),
      panel: q('.itb-panel'), note: q('.itb-note'), toolbar: q('.itb-toolbar'), menu: q('.itb-menu'),
      explode: q('.itb-explode input'), showAll: q('.itb-showall'), close: q('.itb-close'),
      filament: q('[data-act="filament"]'), assemble: q('[data-act="assemble"]'),
    };

    const r = this.renderer = new THREE.WebGLRenderer({ antialias: true });
    r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    r.shadowMap.enabled = true;
    r.shadowMap.type = THREE.PCFSoftShadowMap;
    r.toneMapping = THREE.ACESFilmicToneMapping;
    r.toneMappingExposure = 1.05;
    this.ui.canvas.append(r.domElement);
    this.labels = new CSS2DRenderer();
    this.labels.domElement.className = 'itb-labels';
    this.ui.canvas.append(this.labels.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(BG);
    const pmrem = new THREE.PMREMGenerator(r);
    this.scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    pmrem.dispose();
    this.camera = new THREE.PerspectiveCamera(32, 1, 1, 20000);
    const c = this.controls = new OrbitControls(this.camera, r.domElement);
    Object.assign(c, { enableDamping: true, dampingFactor: 0.08, rotateSpeed: 0.75, zoomSpeed: 0.9,
      screenSpacePanning: true, maxPolarAngle: Math.PI / 2 - 0.03 });
    c.addEventListener('start', () => { this.camTween = null; });

    this.scene.add(new THREE.HemisphereLight(0xffffff, 0xd9d0c3, 0.8));
    this.key = new THREE.DirectionalLight(0xffffff, 1.7);
    Object.assign(this.key.shadow, { bias: -0.0004, normalBias: 0.6 });
    this.key.castShadow = true;
    this.key.shadow.mapSize.set(2048, 2048);
    this.scene.add(this.key, this.key.target);
    const fill = new THREE.DirectionalLight(0xfff4e6, 0.45);         // front-left fill keeps front faces readable
    fill.position.set(-0.8, 0.5, 1);
    const rim = new THREE.DirectionalLight(0xffffff, 0.25);
    rim.position.set(0.3, 0.7, -1);
    this.scene.add(fill, rim);

    this.root = new THREE.Group();                 // box frame: x = width, y = length, z = up (mm)
    this.root.rotation.x = -Math.PI / 2;
    this.scene.add(this.root);
    this.organizer = new THREE.Group();
    this.root.add(this.organizer);
    this.ground = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.ShadowMaterial({ opacity: 0.17 }));
    this.ground.receiveShadow = true;
    this.root.add(this.ground);

    this.geos = unitGeometries();
    this.loader = new STLLoader();
    this.raycaster = new THREE.Raycaster();
    this.trays = [];
    this.selected = this.hovered = this.isolated = null;
    this.explode = this.explodeTarget = 0;
    this.showContents = store.get('contents', false);
    this.showBox = true;
    this.palette = store.get('filament', 'set');
    this.compact = false;
    this._gen = 0;

    this._bindUI();
    this._bindPointer();
    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.el);
    this.setCompact(this.opts.compact);
    this._frame = this._frame.bind(this);
    requestAnimationFrame(this._frame);
  }

  // ------------------------------------------------------------------ loading
  async load({ layout = null, trays = null, stlUrl = (t) => t.stl_url || t.url || t.stl, title = '' } = {}) {
    const gen = ++this._gen;
    this._clear();
    const L = this.layout = layout || {};
    const list = trays || L.trays || [];
    const b = L.box_inner_mm || {};
    const ext = list.map((t, i) => placementOf(t, i).a);
    this.box = {
      W: Number(b.width) || Math.max(100, ...ext.map((v) => v.x * 2)),
      L: Number(b.length) || Math.max(100, ...ext.map((v) => v.y * 2)),
      H: Number(b.height) || Math.max(30, ...list.map((t) => (t.outer_mm?.height || 0))),
    };
    this.organizer.position.set(-this.box.W / 2, -this.box.L / 2, 0);
    this.trays = list.map((t, i) => this._makeTray(t, i, stlUrl(t, i), gen));
    this._buildBox(L);
    this._setTitle(title || L.game || 'Organizer');
    await Promise.all(this.trays.map((tr) => tr.loading));
    if (gen !== this._gen) return;
    this._applyPalette();
    this._applyVisibility();
    this._applyContents();
    this._fitLights();
    this.setCameraPreset('iso', false);
  }

  _makeTray(t, i, url, gen) {
    const { a, e, rot } = placementOf(t, i);
    const group = new THREE.Group();
    group.position.copy(a);
    group.rotation.z = THREE.MathUtils.degToRad(rot);
    this.organizer.add(group);
    const material = new THREE.MeshStandardMaterial({ color: SET_COLORS[i % SET_COLORS.length], roughness: 0.6,
      envMapIntensity: 0.55 });
    const edgeMat = new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.16 });
    const tr = { data: t, index: i, group, a, e, url, material, edgeMat, mesh: null, edges: null, hidden: false,
      ghost: false, order: Number.isFinite(t.assembly_order) ? t.assembly_order : i, contents: null };
    tr.loading = new Promise((resolve) => {
      const done = (geo, failed = false) => {
        if (gen !== this._gen) { geo.dispose(); return resolve(); }
        tr.failed = failed;
        tr.mesh = new THREE.Mesh(geo, material);
        tr.mesh.castShadow = tr.mesh.receiveShadow = true;
        tr.mesh.userData.tray = tr;
        tr.edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 30), edgeMat);
        tr.edges.raycast = () => {};
        group.add(tr.mesh, tr.edges);
        resolve();
      };
      if (!url) {
        const o = t.outer_mm || {};
        return done(new THREE.BoxGeometry(o.width || 40, o.length || 40, o.height || 20)
          .translate(0, 0, (o.height || 20) / 2), true);
      }
      this.loader.load(String(url), (geo) => done(geo), undefined, () => {
        const o = t.outer_mm || {};                 // keep the layout readable if one STL is missing
        done(new THREE.BoxGeometry(o.width || 40, o.length || 40, o.height || 20).translate(0, 0, (o.height || 20) / 2), true);
      });
    });
    return tr;
  }

  _buildBox(L) {
    const { W, L: Ln, H } = this.box;
    const g = new THREE.Group();
    const geo = new THREE.BoxGeometry(W, Ln, H).translate(W / 2, Ln / 2, H / 2);
    const walls = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color: 0x8a7f72, transparent: true, opacity: 0.05,
      depthWrite: false, side: THREE.BackSide }));
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo),
      new THREE.LineBasicMaterial({ color: 0x6f665e, transparent: true, opacity: 0.38 }));
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(W, Ln).translate(W / 2, Ln / 2, -0.15),
      new THREE.MeshStandardMaterial({ color: 0xddd5c8, roughness: 0.95 }));
    floor.receiveShadow = true;
    for (const o of [walls, edges, floor]) o.raycast = () => {};
    g.add(walls, edges, floor);
    for (const b of L.bottom_layer || []) {             // non-printed items under the trays (hand-written plans)
      const [z0, z1] = b.z_range_mm || [0, 0];
      const p = b.assumed_position_mm || { x: 0, y: 0 };
      const bw = W - 2 * p.x, bl = Ln - 2 * p.y;
      const slab = new THREE.Mesh(new THREE.BoxGeometry(bw, bl, Math.max(0.5, z1 - z0)).translate(p.x + bw / 2, p.y + bl / 2, (z0 + z1) / 2),
        new THREE.MeshStandardMaterial({ color: 0x5b7a64, roughness: 0.9 }));
      slab.receiveShadow = true;
      slab.raycast = () => {};
      g.add(slab);
    }
    this.boxGhost = g;
    g.visible = this.showBox;
    this.organizer.add(g);
  }

  _clear() {
    this.anim = this.camTween = null;
    this.selected = this.hovered = this.isolated = null;
    this.explode = this.explodeTarget = 0;
    this.ui.explode.value = '0';
    this._removeDims();
    const shared = new Set(Object.values(this.geos));
    for (const tr of this.trays) {
      tr.group.traverse((o) => {
        if (o.geometry && !shared.has(o.geometry)) o.geometry.dispose();
        if (o.isInstancedMesh) { o.material.dispose(); o.dispose(); }
      });
      tr.material.dispose();
      tr.edgeMat.dispose();
      this.organizer.remove(tr.group);
    }
    if (this.boxGhost) { this.organizer.remove(this.boxGhost); this.boxGhost = null; }
    this.trays = [];
    this.ui.panel.hidden = true;
  }

  _setTitle(name) {
    const { W, L, H } = this.box;
    const n = this.trays.length;
    this.ui.title.querySelector('strong').textContent = name;
    this.ui.title.querySelector('span').textContent =
      `${n} printable box${n === 1 ? '' : 'es'} · game box ${mm(W)} × ${mm(L)} × ${mm(H)} mm`;
  }

  // ------------------------------------------------------------------ appearance
  setPalette(id) {
    this.palette = FILAMENTS.some((f) => f.id === id) ? id : 'set';
    store.set('filament', this.palette);
    this._applyPalette();
  }

  _applyPalette() {
    const f = FILAMENTS.find((p) => p.id === this.palette) || FILAMENTS[0];
    for (const tr of this.trays) {
      const m = tr.material;
      if (f.id === 'set') m.color.set(SET_COLORS[tr.index % SET_COLORS.length]);
      else m.color.set(f.color).offsetHSL(0, 0, ((tr.index % 3) - 1) * 0.022);   // tell same-colour boxes apart
      m.roughness = f.roughness ?? 0.6;
      m.metalness = f.metalness ?? 0;
      m.needsUpdate = true;
    }
    this.ui.filament.querySelector('.itb-swatch').style.background = f.swatch || f.color;
    this._renderMenu();
    this._highlight();
    this._renderPanel();
  }

  setShowBox(on) {
    this.showBox = !!on;
    if (this.boxGhost) this.boxGhost.visible = this.showBox;
    this.el.querySelector('[data-toggle="box"]').setAttribute('aria-pressed', String(this.showBox));
  }

  setShowContents(on) {
    this.showContents = !!on;
    store.set('contents', this.showContents);
    this._applyContents();
    this._renderPanel();
  }

  _applyContents() {
    for (const tr of this.trays) {
      if (this.showContents && !tr.contents) tr.contents = this._buildContents(tr);
      if (tr.contents) tr.contents.visible = this.showContents && !tr.ghost;
    }
    this.el.querySelector('[data-toggle="contents"]').setAttribute('aria-pressed', String(this.showContents));
    const any = this.trays.some((tr) => tr.contentsInfo && tr.contentsInfo.pieces.length);
    this.ui.note.hidden = !(this.showContents && any) || this.compact;
    this.ui.note.textContent = 'Contents are approximate stand-ins, drawn as each box was sized (bounding shapes, '
      + 'not a verified packing). Not printed.';
  }

  _buildContents(tr) {
    const res = tr.contentsInfo = contentsProxies(tr.data);
    const group = new THREE.Group();
    const buckets = new Map();
    for (const p of res.pieces) {
      const key = `${p.member}:${p.shape}`;
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(p);
    }
    const m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), v = new THREE.Vector3(), s = new THREE.Vector3();
    for (const [key, list] of buckets) {
      const [mi, shape] = key.split(':');
      const card = shape === 'card';
      const mat = new THREE.MeshStandardMaterial({
        color: card ? CARD_COLORS[mi % CARD_COLORS.length] : PIECE_COLORS[mi % PIECE_COLORS.length],
        roughness: card ? 0.9 : 0.55, envMapIntensity: 0.6 });
      const mesh = new THREE.InstancedMesh(this.geos[shape] || this.geos.box, mat, list.length);
      list.forEach((p, k) => {
        const h = card ? p.sz * 0.8 : p.sz;                         // a hairline gap shows the card stack
        mesh.setMatrixAt(k, m4.compose(v.set(p.x, p.y, p.z + h / 2), q, s.set(p.sx, p.sy, h)));
      });
      mesh.castShadow = mesh.receiveShadow = true;
      mesh.raycast = () => {};
      group.add(mesh);
    }
    tr.group.add(group);
    return group;
  }

  // ------------------------------------------------------------------ selection, visibility
  select(index, { frame = false, silent = false } = {}) {
    const tr = index == null ? null : this.trays[index] || null;
    this.selected = tr && !tr.hidden ? tr : null;
    this._applyLook();
    this._highlight();
    this._buildDims();
    this._renderPanel();
    if (frame && this.selected) this._frameObject(this.selected.group);
    if (!silent) this.opts.onSelect?.(this.selected ? this.selected.index : null);
  }

  isolate(index) {
    const tr = this.trays[index];
    this.isolated = this.isolated === tr ? null : tr;
    if (this.isolated) this.isolated.hidden = false;
    this._applyVisibility();
  }

  hide(index) {
    const tr = this.trays[index];
    if (!tr) return;
    tr.hidden = true;
    if (this.isolated === tr) this.isolated = null;
    if (this.selected === tr) this.select(null);
    this._applyVisibility();
  }

  showAll() {
    for (const tr of this.trays) tr.hidden = false;
    this.isolated = null;
    this._applyVisibility();
  }

  _applyVisibility() {
    for (const tr of this.trays) {
      tr.group.visible = !tr.hidden;
      tr.ghost = !!this.isolated && this.isolated !== tr;
    }
    this._applyLook();
    const hidden = this.trays.filter((t) => t.hidden).length;
    this.ui.showAll.hidden = !(hidden || this.isolated) || this.compact;
    this.ui.showAll.textContent = this.isolated ? 'Isolated · Show all' : `${hidden} hidden · Show all`;
    this._highlight();
    this._renderPanel();
  }

  _highlight() {
    for (const tr of this.trays) {
      const sel = tr === this.selected, hov = tr === this.hovered && !sel;
      tr.material.emissive.copy(tr.material.color);
      tr.material.emissiveIntensity = sel ? 0.1 : hov ? 0.2 : 0;
      tr.edgeMat.color.setHex(sel ? ACCENT : INK);
      tr.edgeMat.opacity = sel ? 1 : tr.ghost ? 0.05 : tr.xray ? 0.3 : hov ? 0.4 : 0.16;
    }
  }

  // ------------------------------------------------------------------ dimension lines (selected box only)
  _removeDims() {
    if (!this.dims) return;
    this.dims.traverse((o) => { if (o.isCSS2DObject) o.element.remove(); o.geometry?.dispose(); });
    this.dims.parent?.remove(this.dims);
    this.dims = null;
  }

  _buildDims() {
    this._removeDims();
    const tr = this.selected;
    if (!tr || this.compact) return;
    const o = tr.data.outer_mm || {};
    const w = +o.width, l = +o.length, h = +o.height;
    if (!(w > 0 && l > 0 && h > 0)) return;
    const off = clamp(Math.min(w, l) * 0.09, 6, 14), tk = 2.2, z0 = 0.4;
    const yF = -l / 2 - off, xR = w / 2 + off;
    const pts = [];
    const seg = (a, b) => pts.push(new THREE.Vector3(...a), new THREE.Vector3(...b));
    const slash = (x, y, z, axis) => (axis === 'z'
      ? seg([x - tk, y, z - tk], [x + tk, y, z + tk]) : seg([x - tk, y - tk, z], [x + tk, y + tk, z]));
    seg([-w / 2, yF, z0], [w / 2, yF, z0]);                                      // width, in front
    seg([-w / 2, -l / 2, z0], [-w / 2, yF - 3, z0]); seg([w / 2, -l / 2, z0], [w / 2, yF - 3, z0]);
    slash(-w / 2, yF, z0); slash(w / 2, yF, z0);
    seg([xR, -l / 2, z0], [xR, l / 2, z0]);                                       // length, on the right
    seg([w / 2, -l / 2, z0], [xR + 3, -l / 2, z0]); seg([w / 2, l / 2, z0], [xR + 3, l / 2, z0]);
    slash(xR, -l / 2, z0); slash(xR, l / 2, z0);
    seg([xR, yF, 0], [xR, yF, h]);                                                // height, at the corner
    seg([w / 2, -l / 2, h], [xR, yF, h]);
    slash(xR, yF, 0, 'z'); slash(xR, yF, h, 'z');
    const lines = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: INK, transparent: true, opacity: 0.7 }));
    lines.raycast = () => {};
    const g = new THREE.Group();
    g.add(lines);
    const label = (text, x, y, z) => {
      const div = document.createElement('div');
      div.className = 'itb-dim';
      div.innerHTML = `${esc(text)}<span>mm</span>`;
      const obj = new CSS2DObject(div);
      obj.position.set(x, y, z);
      g.add(obj);
    };
    label(mm(w), 0, yF, z0);
    label(mm(l), xR, 0, z0);
    label(mm(h), xR, yF, h / 2);
    tr.group.add(g);
    this.dims = g;
  }

  // ------------------------------------------------------------------ info panel
  _renderPanel() {
    const tr = this.selected;
    const p = this.ui.panel;
    if (!tr || this.compact) { p.hidden = true; return; }
    const t = tr.data;
    const o = t.outer_mm || {};
    const cav = t.cells?.[0]?.cavity_mm || t.cavity_mm;
    const need = t.min_outer_mm;
    const info = tr.contentsInfo || contentsProxies(t);
    tr.contentsInfo = info;
    const colour = `#${tr.material.color.getHexString()}`;
    const where = (t.layer || 0) > 0 ? `stacked, level ${t.layer + 1}` : 'on the box floor';
    const style = STYLE_LABEL[info.style] || STYLE_LABEL[t.storage_style] || '';
    const layers = info.layers > 1 ? ` · ${info.layers} layers` : '';
    const swatch = (i) => (info.pieces.find((q) => q.member === i)?.shape === 'card'
      ? CARD_COLORS[i % CARD_COLORS.length] : PIECE_COLORS[i % PIECE_COLORS.length]);
    const showSwatches = this.showContents && info.pieces.length;
    const members = info.members.length
      ? `<ul class="itb-members">${info.members.map((m, i) => `<li>${showSwatches ? `<i style="background:${swatch(i)}"></i>` : ''}
          <b>${esc(m.quantity)}×</b><span>${esc(m.name)}</span>${m.dims ? `<em>${mm(m.dims.L)} × ${mm(m.dims.W)} × ${mm(m.dims.H)}</em>` : ''}</li>`).join('')}</ul>`
      : (t.contents ? `<p class="itb-muted">${esc(t.contents)}</p>` : '');
    const row = (k, v) => (v ? `<tr><th>${k}</th><td>${v}</td></tr>` : '');
    p.innerHTML = `
      <header><i class="itb-dot" style="background:${colour}"></i><div><h3>${esc(t.name)}</h3>
        <p>Box ${tr.index + 1} of ${this.trays.length} · ${where}${Number(t.rotation_degrees) ? ' · turned 90°' : ''}</p></div>
        <button type="button" class="itb-x" data-panel="close" aria-label="Close details">${ICON.close}</button></header>
      ${style ? `<p class="itb-kicker">${esc(style)}${layers}</p>` : ''}
      ${members}
      <table class="itb-dims">
        ${row('Outer', `${mm(o.width)} × ${mm(o.length)} × ${mm(o.height)} mm`)}
        ${row('Cavity', cav ? `${mm(cav.width)} × ${mm(cav.length)} × ${mm(cav.depth)} mm` : '')}
        ${row('Contents need', need ? `${mm(need.width)} × ${mm(need.length)} × ${mm(need.height)} mm` : '')}
      </table>
      ${this.showContents && info.pieces.length ? `<p class="itb-approx">${esc(info.note)}</p>` : ''}
      ${tr.failed ? '<p class="itb-approx">The STL could not be loaded; an outline of its size is shown.</p>' : ''}
      <div class="itb-actions">
        ${tr.url && !tr.failed ? `<a class="itb-primary" href="${esc(tr.url)}" download="${esc(t.stl || `${t.name}.stl`)}">${ICON.download}Download STL</a>` : ''}
        <button type="button" data-panel="isolate" aria-pressed="${this.isolated === tr}">${this.isolated === tr ? 'Show others' : 'Isolate'}</button>
        <button type="button" data-panel="hide">Hide</button>
      </div>`;
    p.hidden = false;
  }

  // While the panel is open, shift the picture so the panel does not cover the organizer:
  // left on wide screens (side panel), up on narrow ones (bottom sheet).
  _panelShift() {
    if (this.ui.panel.hidden) return [0, 0];
    return this.ui.canvas.clientWidth > 720 ? [(this.ui.panel.offsetWidth + 32) / 2, 0]
      : [0, (this.ui.panel.offsetHeight + 16) / 2];
  }

  _applyViewOffset() {
    const w = this.ui.canvas.clientWidth, h = this.ui.canvas.clientHeight;
    if (!w || !h) return;
    const [x, y] = this.viewShift || [0, 0];
    if (Math.abs(x) < 0.5 && Math.abs(y) < 0.5) this.camera.clearViewOffset();
    else this.camera.setViewOffset(w, h, x, y, w, h);
  }

  // Boxes stacked on top of the selected box turn see-through (until the view is exploded) so it stays visible.
  _covers(tr, sel) {
    if (!sel || tr === sel || (tr.data.layer || 0) <= (sel.data.layer || 0) || this.explode > 0.35) return false;
    const fp = (t) => { const o = t.data.outer_mm || {}; return Number(t.data.rotation_degrees) % 180 ? [o.length, o.width] : [o.width, o.length]; };
    const [aw, al] = fp(tr), [bw, bl] = fp(sel);
    return Math.abs(tr.a.x - sel.a.x) < (aw + bw) / 2 && Math.abs(tr.a.y - sel.a.y) < (al + bl) / 2;
  }

  _applyLook() {
    let changed = false;
    for (const tr of this.trays) {
      const xray = this._covers(tr, this.selected);
      if (xray !== !!tr.xray) changed = true;
      tr.xray = xray;
      const faded = tr.ghost || tr.xray;
      tr.material.transparent = faded;
      tr.material.opacity = tr.ghost ? 0.12 : tr.xray ? 0.2 : 1;
      tr.material.depthWrite = !faded;
      if (tr.mesh) tr.mesh.castShadow = !faded;
      if (tr.contents) tr.contents.visible = this.showContents && !faded;
    }
    if (changed) this._highlight();
  }

  // ------------------------------------------------------------------ explode and assembly
  setExplode(v, { instant = false } = {}) {
    this.anim = null;
    this.ui.assemble.disabled = false;
    this.explodeTarget = clamp(Number(v) || 0, 0, 1.5);
    this.ui.explode.value = String(this.explodeTarget);
    if (instant) { this.explode = this.explodeTarget; this._placeTrays(this.explode); }
  }

  playAssembly() {
    const ranked = [...this.trays].sort((p, q) => p.order - q.order);
    this.anim = { start: performance.now(), rank: new Map(ranked.map((tr, i) => [tr, i])), per: 650,
      stagger: Math.max(120, Math.min(260, 1800 / Math.max(1, ranked.length))) };
    this.ui.assemble.disabled = true;
  }

  _placeTrays(k) {
    for (const tr of this.trays) tr.group.position.lerpVectors(tr.a, tr.e, typeof k === 'function' ? k(tr) : k);
  }

  // ------------------------------------------------------------------ camera
  _bounds(onlyTrays = false) {
    this.scene.updateMatrixWorld(true);
    const box = new THREE.Box3();
    for (const tr of this.trays) if (!tr.hidden && !tr.ghost && tr.mesh) box.expandByObject(tr.mesh);
    if (!onlyTrays && this.boxGhost?.visible) box.expandByObject(this.boxGhost);
    if (box.isEmpty()) box.setFromCenterAndSize(new THREE.Vector3(), new THREE.Vector3(200, 60, 200));
    return box;
  }

  _distanceFor(radius) {
    const vf = THREE.MathUtils.degToRad(this.camera.fov);
    const hf = 2 * Math.atan(Math.tan(vf / 2) * this.camera.aspect);
    return (radius / Math.sin(Math.min(vf, hf) / 2)) * 1.04;
  }

  _aim(dir, box, animate = true) {
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const r = Math.max(sphere.radius, 20);
    const pos = sphere.center.clone().add(dir.clone().normalize().multiplyScalar(this._distanceFor(r)));
    this.camera.near = r / 40;
    this.camera.far = r * 60;
    this.camera.updateProjectionMatrix();
    this.controls.minDistance = r * 0.25;
    this.controls.maxDistance = r * 9;
    if (!animate) {
      this.camTween = null;
      this.camera.position.copy(pos);
      this.controls.target.copy(sphere.center);
      this.controls.update();
      return;
    }
    this.camTween = { p0: this.camera.position.clone(), p1: pos, t0: this.controls.target.clone(),
      t1: sphere.center.clone(), start: performance.now(), dur: 650 };
  }

  setCameraPreset(name, animate = true) {
    const d = VIEW_DIRS[name] || VIEW_DIRS.iso;
    this.view = name;
    for (const b of this.el.querySelectorAll('[data-view]')) b.setAttribute('aria-pressed', String(b.dataset.view === name));
    this._aim(new THREE.Vector3(...d), this._bounds(), animate);
  }

  fitView() {
    this._aim(this.camera.position.clone().sub(this.controls.target), this._bounds());
  }

  _frameObject(obj) {
    const box = new THREE.Box3().setFromObject(obj);
    const dir = this.camera.position.clone().sub(this.controls.target);
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    sphere.radius *= 1.6;                          // keep some context around the box
    const b = new THREE.Box3().setFromCenterAndSize(sphere.center, new THREE.Vector3(1, 1, 1).multiplyScalar(sphere.radius * 1.15));
    this._aim(dir, b);
  }

  resetView() {
    this.anim = null;
    this.setExplode(0);
    this.showAll();
    this.select(null);
    this.setCameraPreset('iso');
  }

  _fitLights() {
    const box = this._bounds();
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const r = Math.max(sphere.radius, 50);
    const c = sphere.center;
    this.key.position.set(c.x + r * 0.9, c.y + r * 1.9, c.z + r * 0.8);
    this.key.target.position.copy(c);
    const cam = this.key.shadow.camera;
    Object.assign(cam, { left: -r * 1.6, right: r * 1.6, top: r * 1.6, bottom: -r * 1.6, near: r * 0.2, far: r * 6 });
    cam.updateProjectionMatrix();
    this.ground.scale.set(r * 10, r * 10, 1);
    this.ground.position.set(0, 0, -0.3);
  }

  // ------------------------------------------------------------------ layout modes
  setCompact(compact) {
    this.compact = !!compact;
    this.el.classList.toggle('is-compact', this.compact);
    this.ui.close.hidden = this.compact || !this.opts.onClose;
    this.ui.menu.hidden = true;
    this._buildDims();
    this._renderPanel();
    this._applyVisibility();
    this._applyContents();
    this.resize();
  }

  resize() {
    const w = this.ui.canvas.clientWidth, h = this.ui.canvas.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.renderer.domElement.style.width = `${w}px`;
    this.renderer.domElement.style.height = `${h}px`;
    this.labels.setSize(w, h);
    this.camera.aspect = w / h;
    this._applyViewOffset();
    this.camera.updateProjectionMatrix();
  }

  // ------------------------------------------------------------------ UI wiring
  _renderMenu() {
    this.ui.menu.innerHTML = FILAMENTS.map((f) => `<button type="button" role="menuitemradio" data-filament="${f.id}"
      aria-checked="${f.id === this.palette}"><i class="itb-swatch" style="background:${f.swatch || f.color}"></i>${esc(f.label)}</button>`).join('');
  }

  _toggleMenu(open = this.ui.menu.hidden) {
    this.ui.menu.hidden = !open;
    this.ui.filament.setAttribute('aria-expanded', String(open));
    if (!open) return;
    const sr = this.ui.stage.getBoundingClientRect(), br = this.ui.filament.getBoundingClientRect();
    this.ui.menu.style.left = `${clamp(br.left - sr.left + br.width / 2 - 90, 8, sr.width - 188)}px`;
    this.ui.menu.style.bottom = `${sr.bottom - br.top + 8}px`;
  }

  _bindUI() {
    this.ui.toolbar.addEventListener('click', (ev) => {
      const b = ev.target.closest('button');
      if (!b) return;
      if (b.dataset.view) this.setCameraPreset(b.dataset.view);
      else if (b.dataset.act === 'fit') this.fitView();
      else if (b.dataset.act === 'reset') this.resetView();
      else if (b.dataset.act === 'assemble') this.playAssembly();
      else if (b.dataset.act === 'filament') this._toggleMenu();
      else if (b.dataset.toggle === 'contents') this.setShowContents(!this.showContents);
      else if (b.dataset.toggle === 'box') this.setShowBox(!this.showBox);
    });
    this.ui.explode.addEventListener('input', () => this.setExplode(this.ui.explode.value));
    this.ui.menu.addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-filament]');
      if (!b) return;
      this.setPalette(b.dataset.filament);
      this._toggleMenu(false);
    });
    this.ui.panel.addEventListener('click', (ev) => {
      const b = ev.target.closest('[data-panel]');
      if (!b || !this.selected) return;
      const i = this.selected.index;
      if (b.dataset.panel === 'close') this.select(null);
      else if (b.dataset.panel === 'isolate') this.isolate(i);
      else if (b.dataset.panel === 'hide') this.hide(i);
    });
    this.ui.showAll.addEventListener('click', () => this.showAll());
    this.ui.close.addEventListener('click', () => this.opts.onClose?.());
    this.el.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Escape' || this.compact) return;
      if (!this.ui.menu.hidden) this._toggleMenu(false);
      else if (this.selected) this.select(null);
      else this.opts.onClose?.();
    });
    this.el.tabIndex = -1;
  }

  _pick(ev) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    const meshes = this.trays.filter((t) => t.mesh && !t.hidden && !t.ghost && !t.xray).map((t) => t.mesh);
    return this.raycaster.intersectObjects(meshes, false)[0]?.object.userData.tray || null;
  }

  _bindPointer() {
    const el = this.renderer.domElement;
    el.addEventListener('pointerdown', (ev) => {
      this.down = { x: ev.clientX, y: ev.clientY, t: performance.now() };
      this.ui.menu.hidden = true;
      this.el.focus({ preventScroll: true });
    });
    el.addEventListener('pointerup', (ev) => {
      const d = this.down;
      this.down = null;
      if (!d || Math.hypot(ev.clientX - d.x, ev.clientY - d.y) > 5 || performance.now() - d.t > 600) return;
      const tr = this._pick(ev);
      this.select(tr ? tr.index : null);
    });
    el.addEventListener('pointermove', (ev) => { this.pointer = ev; this.hoverDirty = true; });
    el.addEventListener('pointerleave', () => { this.pointer = null; this.hoverDirty = true; });
  }

  _updateHover() {
    this.hoverDirty = false;
    const ev = this.pointer;
    const tr = ev && !this.down && ev.pointerType !== 'touch' ? this._pick(ev) : null;
    if (tr !== this.hovered) { this.hovered = tr; this._highlight(); }
    this.renderer.domElement.style.cursor = tr ? 'pointer' : '';
    const h = this.ui.hover;
    if (!tr) { h.hidden = true; return; }
    const sr = this.ui.stage.getBoundingClientRect();
    h.textContent = tr.data.name;
    h.style.left = `${ev.clientX - sr.left}px`;
    h.style.top = `${ev.clientY - sr.top}px`;
    h.hidden = false;
  }

  // ------------------------------------------------------------------ frame loop
  _frame(now) {
    requestAnimationFrame(this._frame);
    const dt = Math.min(0.05, (now - (this.last || now)) / 1000);
    this.last = now;
    if (!this.el.isConnected || !this.ui.canvas.clientWidth) return;
    if (this.anim) {
      const { start, rank, per, stagger } = this.anim;
      const T = now - start;
      const total = per + stagger * Math.max(rank.size - 1, 0);
      this._placeTrays((tr) => 1.15 * (1 - ease(clamp((T - rank.get(tr) * stagger) / per, 0, 1))));
      this.ui.explode.value = String(clamp(1.15 * (1 - T / total), 0, 1.5));
      if (T > total) {
        this.anim = null;
        this.explode = this.explodeTarget = 0;
        this.ui.explode.value = '0';
        this.ui.assemble.disabled = false;
      }
    } else if (Math.abs(this.explodeTarget - this.explode) > 1e-4) {
      this.explode += (this.explodeTarget - this.explode) * (1 - Math.exp(-dt * 9));
      if (Math.abs(this.explodeTarget - this.explode) < 1e-3) this.explode = this.explodeTarget;
      this._placeTrays(this.explode);
      if (this.selected) this._applyLook();
    }
    if (this.camTween) {
      const k = ease(clamp((now - this.camTween.start) / this.camTween.dur, 0, 1));
      this.camera.position.lerpVectors(this.camTween.p0, this.camTween.p1, k);
      this.controls.target.lerpVectors(this.camTween.t0, this.camTween.t1, k);
      if (k >= 1) this.camTween = null;
    }
    const want = this._panelShift(), cur = this.viewShift || [0, 0];
    if (Math.abs(want[0] - cur[0]) > 0.3 || Math.abs(want[1] - cur[1]) > 0.3) {
      const k = 1 - Math.exp(-dt * 10);
      this.viewShift = cur.map((v, i) => (Math.abs(want[i] - v) <= 0.3 ? want[i] : v + (want[i] - v) * k));
      this._applyViewOffset();
    }
    if (this.hoverDirty) this._updateHover();
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
  }
}
