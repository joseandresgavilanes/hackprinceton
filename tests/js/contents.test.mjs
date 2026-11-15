// node --test tests/js   (run by tests/test_viewer_contents.py)
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import { test } from 'node:test';
import { contentsProxies, heightFor, proxyShape } from '../../frontend/viewer/contents.js';

const root = new URL('../../', import.meta.url);
const read = (rel) => JSON.parse(readFileSync(new URL(rel, root), 'utf-8'));
const LAYOUTS = ['holden_final/king_of_tokyo_5_boxes/layout.json', 'holden_final/ttr_london_6_boxes/layout.json',
  'holden_final/exoutput_3_boxes/layout.json', 'holden_final/exoutput_1_4_boxes/layout.json'];
const EPS = 0.02;

function inside(p, cav) {
  const [hx, hy] = [p.sx / 2, p.sy / 2];
  return p.x - hx >= cav.cx - cav.w / 2 - EPS && p.x + hx <= cav.cx + cav.w / 2 + EPS
    && p.y - hy >= cav.cy - cav.l / 2 - EPS && p.y + hy <= cav.cy + cav.l / 2 + EPS
    && p.z >= cav.floor - EPS && p.z + p.sz <= cav.top + EPS;
}

for (const rel of LAYOUTS) {
  test(`stand-ins stay inside every cavity and match the quantities: ${rel}`, () => {
    for (const tray of read(rel).trays) {
      const c = tray.cells[0];
      const cav = { cx: c.cavity_center_mm[0], cy: c.cavity_center_mm[1], w: c.cavity_mm.width, l: c.cavity_mm.length,
        floor: tray.outer_mm.height - c.cavity_mm.depth, top: tray.outer_mm.height };
      const res = contentsProxies(tray);
      assert.notEqual(res.mode, 'none', tray.name);
      assert.ok(res.note.startsWith('Approximate'), tray.name);
      c.contents.members.forEach((m, i) => assert.equal(res.shown[i], m.quantity, `${tray.name}: ${m.name}`));
      for (const p of res.pieces) assert.ok(inside(p, cav), `${tray.name}: ${JSON.stringify(p)}`);
      // pieces of one layer never overlap (decks and stacks are one column by design)
      if (res.mode === 'layout') {
        for (let i = 0; i < res.pieces.length; i += 1) {
          for (let j = i + 1; j < res.pieces.length; j += 1) {
            const a = res.pieces[i], b = res.pieces[j];
            if (Math.abs(a.z - b.z) > EPS) continue;
            const ox = Math.min(a.x + a.sx / 2, b.x + b.sx / 2) - Math.max(a.x - a.sx / 2, b.x - b.sx / 2);
            const oy = Math.min(a.y + a.sy / 2, b.y + b.sy / 2) - Math.max(a.y - a.sy / 2, b.y - b.sy / 2);
            assert.ok(ox <= EPS || oy <= EPS, `${tray.name}: pieces ${i} and ${j} overlap`);
          }
        }
      }
    }
  });
}

test('King of Tokyo: the Monster Kits box shows 3 layers, the deck is one flat stack of 66 cards', () => {
  const trays = read('holden_final/king_of_tokyo_5_boxes/layout.json').trays;
  const kits = contentsProxies(trays.find((t) => t.name.startsWith('Monster Kits')));
  assert.equal(kits.mode, 'layout');
  assert.equal(new Set(kits.pieces.map((p) => p.z.toFixed(2))).size, 3);
  const deck = contentsProxies(trays.find((t) => t.name === 'Power Card Deck'));
  assert.equal(deck.mode, 'deck');
  assert.equal(deck.pieces.length, 66);
  assert.ok(deck.pieces.every((p) => p.shape === 'card' && p.x === deck.pieces[0].x));
  const dice = contentsProxies(trays.find((t) => t.name === 'Dice Pool'));
  assert.ok(dice.pieces.every((p) => p.shape === 'box'), 'dice are drawn as cubes');
});

test('helpers: footprint vs height and shape mapping', () => {
  assert.equal(heightFor({ L: 24.2, W: 5.5, H: 15.1 }, 24.2, 5.5), 15.1);
  assert.equal(heightFor({ L: 40, W: 30, H: 60 }, 60, 40), 30);          // laid on its side
  assert.equal(proxyShape({ name: 'Black die', shape: 'cylinder' }), 'box');
  assert.equal(proxyShape({ name: 'Poison token', shape: 'cylinder' }), 'cylinder');
  assert.equal(proxyShape({ name: 'Hex tile', shape: 'hexagonal_prism' }), 'hex');
});

test('typed tray layouts without member data show nothing instead of guessing', () => {
  for (const rel of ['generated_organizer/layout.json', 'examples/output/stacked_demo/layout.json']) {
    if (!existsSync(new URL(rel, root))) continue;
    for (const tray of read(rel).trays) assert.equal(contentsProxies(tray).pieces.length, 0, `${rel}: ${tray.name}`);
  }
});
