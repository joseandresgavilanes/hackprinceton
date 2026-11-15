// Approximate, non-printable stand-ins for the pieces a storage box holds (visualization only).
//
// Coordinates are the box's own STL frame in millimetres: x/y centred on the box, z = 0 at its base.
// Nothing here changes a printable part or claims a verified packing:
// - "loose" / "tight_pack" boxes use the arrangement the engine sized the cavity for
//   (contents.arrangement_mm, repeated in contents.layers identical layers): bounding boxes, not real shapes;
// - "card_stack" is the deck lying flat, "stack" one vertical stack, as the box was sized;
// - anything else falls back to an illustrative grid and says so.

const TOL = 0.05;
const CUBE = /\b(die|dice|cubes?)\b/i;      // grouping JSON often lists dice and cubes as cylinder envelopes

export const NOTES = {
  layout: 'Approximate: simple shapes placed as the engine sized this box (bounding boxes, not exact pieces). Not a verified packing.',
  deck: 'Approximate: the deck lying flat, as the box was sized.',
  stack: 'Approximate: one vertical stack, as the box was sized.',
  illustrative: 'Illustrative only: this layout has no stored arrangement, so the pieces are spread out to show what the box holds.',
  none: 'No component data for this box.',
};

export function proxyShape(member) {
  const shape = String(member.shape || '').toLowerCase();
  if (CUBE.test(member.name || '')) return 'box';
  if (shape === 'cylinder') return 'cylinder';
  if (shape === 'sphere') return 'sphere';
  if (shape === 'cone') return 'cone';
  if (shape === 'hexagonal_prism') return 'hex';
  if (shape === 'triangular_prism') return 'tri';
  return 'box';
}

function dimsOf(member) {
  const d = member.dimensions_mm || {};
  const L = Number(d.length), W = Number(d.width), H = Number(d.height);
  return [L, W, H].every((v) => Number.isFinite(v) && v > 0) ? { L, W, H } : null;
}

// The member dimension that is not the footprint (pieces too tall to stand were laid on their side).
export function heightFor(d, w, l) {
  const rest = [d.L, d.W, d.H];
  for (const v of [w, l]) {
    let best = -1;
    let err = Infinity;
    rest.forEach((r, i) => { const e = Math.abs(r - v); if (e < err) { err = e; best = i; } });
    if (best >= 0 && err <= Math.max(TOL, v * 0.01)) rest.splice(best, 1);
  }
  return rest.length === 1 ? rest[0] : d.H;
}

export function cavityOf(tray) {
  const cell = (tray.cells || [])[0];
  const c = cell?.cavity_mm || {};
  const H = Number(tray.outer_mm?.height);
  if (!(c.width > 0 && c.length > 0 && c.depth > 0 && H > 0)) return null;
  const [cx, cy] = cell.cavity_center_mm || [0, 0];
  return { cx, cy, w: c.width, l: c.length, depth: c.depth, floor: H - c.depth, top: H };
}

function piece(member, shape, x, y, z, sx, sy, sz) {
  if (shape === 'cylinder' || shape === 'hex' || shape === 'tri' || shape === 'sphere' || shape === 'cone') {
    const d = Math.min(sx, sy);
    sx = d; sy = d;
  }
  return { member, shape, x, y, z, sx, sy, sz };
}

function deck(members, cav) {
  const pieces = [];
  let z = cav.floor;
  members.forEach((m, mi) => {
    const d = dimsOf(m);
    if (!d) return;
    const long = Math.max(d.L, d.W), short = Math.min(d.L, d.W);
    const [sx, sy] = cav.w >= cav.l ? [long, short] : [short, long];   // card's long side along the cavity's
    for (let i = 0; i < m.quantity; i += 1) {
      pieces.push({ member: mi, shape: 'card', x: cav.cx, y: cav.cy, z, sx, sy, sz: d.H });
      z += d.H;
    }
  });
  return pieces;
}

function stack(members, cav) {
  const pieces = [];
  let z = cav.floor;
  members.forEach((m, mi) => {
    const d = dimsOf(m);
    if (!d) return;
    const shape = proxyShape(m);
    for (let i = 0; i < m.quantity; i += 1) {
      pieces.push(piece(mi, shape, cav.cx, cav.cy, z, d.W, d.L, d.H));
      z += d.H;
    }
  });
  return pieces;
}

function arranged(members, contents, cav) {
  const arr = contents.arrangement_mm;
  const layers = Math.max(1, Number(contents.layers) || 1);
  const index = new Map(members.map((m, i) => [m.name, i]));
  const ax = Math.max(...arr.map((a) => a.x + a.w));
  const ay = Math.max(...arr.map((a) => a.y + a.l));
  const fits = (x, y) => x <= cav.w + TOL && y <= cav.l + TOL;
  const transpose = !fits(ax, ay) && fits(ay, ax);
  const ex = transpose ? ay : ax, ey = transpose ? ax : ay;
  const x0 = cav.cx - ex / 2, y0 = cav.cy - ey / 2;            // centred in the (possibly larger) cavity
  const slots = [];
  for (const a of arr) {
    const mi = index.get(a.member);
    const d = mi === undefined ? null : dimsOf(members[mi]);
    if (!d) continue;
    slots.push({ mi, a, h: heightFor(d, a.w, a.l) });
  }
  if (!slots.length) return [];
  const layerH = Math.max(...slots.map((s) => s.h));
  const placed = members.map(() => 0);
  const pieces = [];
  for (let j = 0; j < layers; j += 1) {
    for (const { mi, a, h } of slots) {
      if (placed[mi] >= members[mi].quantity) continue;
      placed[mi] += 1;
      const [px, py, sx, sy] = transpose
        ? [a.y + a.l / 2, a.x + a.w / 2, a.l, a.w]
        : [a.x + a.w / 2, a.y + a.l / 2, a.w, a.l];
      pieces.push(piece(mi, proxyShape(members[mi]), x0 + px, y0 + py, cav.floor + j * layerH, sx, sy, h));
    }
  }
  return pieces;
}

// No stored arrangement: simple shelf rows, layer by layer, stopping at the cavity's depth.
function illustrative(members, cav) {
  const pieces = [];
  const gap = 0.6;
  let x = 0, y = 0, z = 0, rowL = 0, layerH = 0;
  for (const [mi, m] of members.entries()) {
    const d = dimsOf(m);
    if (!d) continue;
    const shape = proxyShape(m);
    for (let i = 0; i < m.quantity; i += 1) {
      if (x + d.W > cav.w) { x = 0; y += rowL + gap; rowL = 0; }
      if (y + d.L > cav.l) { y = 0; x = 0; z += layerH; layerH = 0; rowL = 0; }
      if (z + d.H > cav.depth || d.W > cav.w || d.L > cav.l) return pieces;
      pieces.push(piece(mi, shape, cav.cx - cav.w / 2 + x + d.W / 2, cav.cy - cav.l / 2 + y + d.L / 2,
        cav.floor + z, d.W, d.L, d.H));
      x += d.W + gap; rowL = Math.max(rowL, d.L); layerH = Math.max(layerH, d.H);
    }
  }
  return pieces;
}

export function contentsProxies(tray) {
  const contents = (tray.cells || [])[0]?.contents || {};
  const members = Array.isArray(contents.members) ? contents.members : [];
  const cav = cavityOf(tray);
  const style = contents.storage_style || null;
  const summary = members.map((m) => ({ name: m.name, quantity: m.quantity, shape: proxyShape(m), dims: dimsOf(m) }));
  if (!cav || !members.length) return { mode: 'none', note: NOTES.none, style, members: summary, pieces: [] };
  let mode, pieces;
  if (style === 'card_stack') { mode = 'deck'; pieces = deck(members, cav); }
  else if (style === 'stack') { mode = 'stack'; pieces = stack(members, cav); }
  else if (Array.isArray(contents.arrangement_mm) && contents.arrangement_mm.length) {
    mode = 'layout'; pieces = arranged(members, contents, cav);
  } else { mode = 'illustrative'; pieces = illustrative(members, cav); }
  const shown = members.map((_, i) => pieces.filter((p) => p.member === i).length);
  return { mode, note: NOTES[mode], style, layers: Number(contents.layers) || null, members: summary, pieces, shown };
}
