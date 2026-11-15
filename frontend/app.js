// The backend endpoint comes from config.js (served by `python -m backend`). Empty keeps the static demo mode.
const ORGANIZER_API_URL = (window.INSIDE_THE_BOX_CONFIG?.apiUrl || '').replace(/\/$/, '');
const DEMO_PROCESSING_MS = 4200;
const ASSET_ROOT = '../generated_organizer/';
const FALLBACK_TRAYS = [
  ['Playing cards', '01_playing_cards.stl'], ['Square tiles', '02_square_tiles.stl'],
  ['Red meeples', '03_red_meeples.stl'], ['Blue meeples', '04_blue_meeples.stl'],
  ['Green meeples', '05_green_meeples.stl'], ['Yellow meeples', '06_yellow_meeples.stl'],
  ['Dice and tokens', '07_dice_and_tokens.stl']
];
const CRC32_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let index = 0; index < 256; index += 1) {
    let value = index;
    for (let bit = 0; bit < 8; bit += 1) value = value & 1 ? (value >>> 1) ^ 0xedb88320 : value >>> 1;
    table[index] = value >>> 0;
  }
  return table;
})();

const $ = (selector) => document.querySelector(selector);
const inputScreen = $('#input-screen'); const reviewScreen = $('#review-screen'); const resultsScreen = $('#results-screen');
const photos = $('#photos'); const photoGrid = $('#photo-grid'); const photoCount = $('#photo-count'); let selectedFiles = [];
let bypassFlow = false; let loadingDemoFlow = false; let currentJob = null; let completedTrays = null; let completedLayout = null; let activeLayout = null;
let loadingAnimationFrame = null; let loadingOrbitStartedAt = 0; let loadingFinishing = false; let loadingExitScheduled = false; let loadingFinishedCallback = null;

function renderPhotos() {
  photoGrid.innerHTML = '';
  selectedFiles.forEach((file, index) => {
    const card = document.createElement('div'); card.className = 'photo-thumb';
    const image = document.createElement('img'); image.src = URL.createObjectURL(file); image.alt = `Component photo ${index + 1}`;
    const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×'; remove.setAttribute('aria-label', `Remove ${file.name}`);
    remove.onclick = () => { selectedFiles.splice(index, 1); renderPhotos(selectedFiles); };
    const name = document.createElement('span'); name.className = 'photo-name'; name.textContent = `Image ${index + 1} · ${file.name}`;
    card.append(image, remove, name); photoGrid.append(card);
  });
  photoCount.textContent = `${selectedFiles.length} / 4 added`;
}
function addPhotos(files) {
  const remainingSlots = 4 - selectedFiles.length;
  if (remainingSlots <= 0) return;
  selectedFiles.push(...[...files].slice(0, remainingSlots));
  renderPhotos();
}
photos.addEventListener('change', (event) => {
  addPhotos(event.target.files);
  // The source of truth is selectedFiles, so allow choosing the same file again if needed.
  event.target.value = '';
});
const dropZone = $('#drop-zone');
['dragenter', 'dragover'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.add('dragging'); }));
['dragleave', 'drop'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => { event.preventDefault(); dropZone.classList.remove('dragging'); }));
dropZone.addEventListener('drop', (event) => addPhotos(event.dataTransfer.files));

function showLoading(message) {
  if (loadingAnimationFrame) cancelAnimationFrame(loadingAnimationFrame);
  const loadingPhotos = $('#loading-photos');
  loadingPhotos.innerHTML = '';
  selectedFiles.forEach((file, index) => {
    const photo = document.createElement('div');
    photo.className = 'loading-photo';
    photo.dataset.orbitIndex = String(index);
    const image = document.createElement('img');
    image.src = URL.createObjectURL(file);
    image.alt = '';
    photo.append(image); loadingPhotos.append(photo);
  });
  $('#loading-copy').textContent = message;
  $('#loading').classList.remove('hidden');
  loadingFinishing = false; loadingExitScheduled = false; loadingFinishedCallback = null;
  loadingOrbitStartedAt = performance.now();
  loadingAnimationFrame = requestAnimationFrame(animateLoadingPhotos);
}
function hideLoading() {
  if (loadingAnimationFrame) cancelAnimationFrame(loadingAnimationFrame);
  loadingAnimationFrame = null;
  $('#loading').classList.add('hidden');
}
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function animateLoadingPhotos(now) {
  const scene = document.querySelector('.loading-scene');
  const photos = [...document.querySelectorAll('.loading-photo')];
  if (!scene || !photos.length) return;
  const period = 6800; const total = photos.length;
  const radiusX = scene.clientWidth * .26; const radiusY = scene.clientHeight * .085;
  const elapsed = (now - loadingOrbitStartedAt) / period;
  photos.forEach((photo, index) => {
    if (photo.dataset.departing === 'true') return;
    const angle = elapsed * Math.PI * 2 - index * (Math.PI * 2 / total);
    const normalizedAngle = ((angle % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2);
    const priorAngle = Number(photo.dataset.lastAngle);
    // A wrap through zero is the top of the plate. A finishing card leaves only there.
    if (loadingFinishing && Number.isFinite(priorAngle) && normalizedAngle < priorAngle) {
      sendPhotoOffPlate(photo, index, total);
      return;
    }
    photo.dataset.lastAngle = String(normalizedAngle);
    const x = Math.sin(angle) * radiusX; const y = -Math.cos(angle) * radiusY;
    photo.style.transform = `translate(${x}px, ${y}px) translate(-50%, -50%) rotate(${Math.sin(angle) * 5}deg)`;
  });
  loadingAnimationFrame = requestAnimationFrame(animateLoadingPhotos);
}
function sendPhotoOffPlate(photo, index, total) {
  photo.dataset.departing = 'true'; photo.classList.add('leaving-plate');
  requestAnimationFrame(() => {
    photo.style.transform = `translate(${(index - (total - 1) / 2) * 20}px, -120vh) rotate(${(index - 1) * 5}deg)`;
  });
  const allHaveLeft = [...document.querySelectorAll('.loading-photo')].every((item) => item.dataset.departing === 'true');
  if (allHaveLeft && !loadingExitScheduled) {
    loadingExitScheduled = true;
    setTimeout(() => {
      if (loadingAnimationFrame) cancelAnimationFrame(loadingAnimationFrame);
      loadingAnimationFrame = null; $('#loading').classList.add('hidden'); loadingFinishedCallback?.();
    }, 900);
  }
}
async function waitForOrganizerJob(jobId) {
  while (true) {
    await pause(1500);
    const response = await fetch(`${ORGANIZER_API_URL}/${jobId}`);
    const job = await response.json();
    if (!response.ok || job.status === 'error') throw new Error(job.message || 'Organizer processing failed');
    if (job.status === 'processing' && job.stage_label) $('#loading-copy').textContent = job.stage_label;
    if (job.status !== 'processing') return job;
  }
}
function finishLoading(next) {
  const orbitingPhotos = [...document.querySelectorAll('.loading-photo')];
  $('#loading-copy').textContent = 'Your organizer is ready!';
  if (!orbitingPhotos.length) { setTimeout(() => { hideLoading(); next(); }, 400); return; }
  loadingFinishedCallback = next;
  loadingFinishing = true;
}

function showScreen(screen) { [inputScreen, reviewScreen, resultsScreen].forEach((item) => item.classList.toggle('hidden', item !== screen)); window.scrollTo({ top: 0, behavior: 'smooth' }); }
async function submitOrganizerJob(gameName) {
  if (!ORGANIZER_API_URL) return { status: 'demo' };
  const payload = new FormData(); payload.append('game_name', gameName);
  selectedFiles.forEach((file) => payload.append('reference_photos[]', file, file.name));
  const response = await fetch(ORGANIZER_API_URL, { method: 'POST', body: payload });
  const job = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(job.message || 'Could not start organizer job');
  return job;
}
function showResults(files = null, layout = null) {
  completedTrays = files;
  completedLayout = layout;
  const count = files?.length || 7;
  $('#result-game').textContent = `${$('#game-name').value.trim() || 'Demo game'} · ${count} printable trays`;
  const downloadAll = $('#download-all'); downloadAll.classList.remove('hidden');
  downloadAll.querySelector('small').textContent = `(${count} STL)`;
  showScreen(resultsScreen); setupViewer();
}
$('#organizer-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const name = $('#game-name').value.trim(); if (!name) return $('#game-name').focus();
  bypassFlow = !ORGANIZER_API_URL && name.toLowerCase() === 'bypass';
  loadingDemoFlow = !ORGANIZER_API_URL && name.toLowerCase() === 'loading';
  if (bypassFlow) { showScreen(reviewScreen); return; }
  if (loadingDemoFlow) { showLoading('Planning your organizer…'); await pause(DEMO_PROCESSING_MS); finishLoading(() => showResults()); return; }
  if (selectedFiles.length !== 4) { alert('Upload all four component photos before continuing.'); return; }
  showLoading(ORGANIZER_API_URL ? 'Uploading your photos…' : 'Analyzing your components…');
  try {
    const job = await submitOrganizerJob(name);
    currentJob = job;
    if (job.status === 'demo') { await pause(DEMO_PROCESSING_MS); finishLoading(() => showResults()); }
    else {
      const completedJob = job.status === 'processing' ? await waitForOrganizerJob(job.job_id) : job;
      if (completedJob.status === 'needs_measurement') { askMeasurement(completedJob.question); hideLoading(); showScreen(reviewScreen); }
      else if (completedJob.status === 'complete' && Array.isArray(completedJob.files)) finishLoading(() => showResults(completedJob.files, completedJob.layout));
      else throw new Error('Unexpected organizer job status');
    }
  } catch (error) { hideLoading(); alert(serviceError(error, 'Could not reach the organizer service. Please try again.')); }
});
function serviceError(error, fallback) {
  // A TypeError means the request never reached the backend; anything else carries the backend's own message.
  return error instanceof TypeError || !error?.message ? fallback : error.message;
}
function askMeasurement(question) {
  if (!question) return;
  $('#reference-object').value = question.object || '';
  $('#reference-width').value = '';
  if (question.suggested_value) $('#reference-width').placeholder = `e.g. ${question.suggested_value}`;
}
$('#back-to-input').addEventListener('click', () => showScreen(inputScreen));
$('#generate-button').addEventListener('click', async () => {
  const object = $('#reference-object').value.trim(); const width = $('#reference-width').value.trim();
  const bypassMeasurement = object.toLowerCase() === 'bypass' && width.toLowerCase() === 'bypass';
  if (bypassFlow) { if (!bypassMeasurement) { alert('Enter Bypass in both fields to continue in demo mode.'); return; } showResults(); return; }
  if (!object || !Number.isFinite(Number(width)) || Number(width) <= 0) { alert('Enter an object and a positive numeric width in millimetres.'); return; }
  if (!currentJob?.job_id || !ORGANIZER_API_URL) { alert('A backend measurement job is required to continue.'); return; }
  showLoading('Packing trays into your game box…');
  try {
    const response = await fetch(`${ORGANIZER_API_URL}/${currentJob.job_id}/measurements`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ object, width_mm: Number(width) }) });
    let job = await response.json().catch(() => ({})); if (!response.ok) throw new Error(job.message || 'Organizer is not ready');
    if (job.status === 'processing') job = await waitForOrganizerJob(job.job_id);
    if (job.status !== 'complete' || !Array.isArray(job.files)) throw new Error('Organizer is not ready');
    finishLoading(() => showResults(job.files, job.layout));
  } catch (error) { hideLoading(); alert(serviceError(error, 'The organizer is still processing. Try again when the backend returns its STL files.')); }
});
$('#start-over').addEventListener('click', () => showScreen(inputScreen));

let viewerReady = false; let renderer; let camera; let controls; let scene; let trayMeshes = []; let currentTray = null;
let THREE; let OrbitControls; let STLLoader;
async function trayData() {
  if (completedTrays) {
    if (completedLayout?.trays) {
      return completedLayout.trays.map((tray, index) => ({ ...tray, stl_url: completedTrays[index]?.stl_url || completedTrays[index]?.url }));
    }
    return completedTrays.map((file) => ({ name: file.name, stl_url: file.stl_url || file.url }));
  }
  try { const response = await fetch(`${ASSET_ROOT}layout.json`); if (!response.ok) throw new Error('layout unavailable'); activeLayout = await response.json(); return activeLayout.trays; }
  catch { return FALLBACK_TRAYS.map(([name, stl]) => ({ name, stl })); }
}
function crc32(bytes) {
  let value = 0xffffffff;
  bytes.forEach((byte) => { value = CRC32_TABLE[(value ^ byte) & 0xff] ^ (value >>> 8); });
  return (value ^ 0xffffffff) >>> 0;
}
function writeZip16(view, offset, value) { view.setUint16(offset, value, true); }
function writeZip32(view, offset, value) { view.setUint32(offset, value, true); }
function createStoredZip(entries) {
  const encoder = new TextEncoder(); const localRecords = []; const centralRecords = []; let offset = 0;
  entries.forEach(({ name, bytes }) => {
    const filename = encoder.encode(name); const checksum = crc32(bytes);
    const local = new Uint8Array(30 + filename.length + bytes.length); const localView = new DataView(local.buffer);
    writeZip32(localView, 0, 0x04034b50); writeZip16(localView, 4, 20); writeZip16(localView, 8, 0); writeZip32(localView, 14, checksum);
    writeZip32(localView, 18, bytes.length); writeZip32(localView, 22, bytes.length); writeZip16(localView, 26, filename.length);
    local.set(filename, 30); local.set(bytes, 30 + filename.length); localRecords.push(local);
    const central = new Uint8Array(46 + filename.length); const centralView = new DataView(central.buffer);
    writeZip32(centralView, 0, 0x02014b50); writeZip16(centralView, 4, 20); writeZip16(centralView, 6, 20); writeZip16(centralView, 8, 0);
    writeZip32(centralView, 16, checksum); writeZip32(centralView, 20, bytes.length); writeZip32(centralView, 24, bytes.length);
    writeZip16(centralView, 28, filename.length); writeZip32(centralView, 42, offset); central.set(filename, 46); centralRecords.push(central); offset += local.length;
  });
  const centralSize = centralRecords.reduce((size, record) => size + record.length, 0); const end = new Uint8Array(22); const endView = new DataView(end.buffer);
  writeZip32(endView, 0, 0x06054b50); writeZip16(endView, 8, entries.length); writeZip16(endView, 10, entries.length); writeZip32(endView, 12, centralSize); writeZip32(endView, 16, offset);
  return new Blob([...localRecords, ...centralRecords, end], { type: 'application/zip' });
}
function zipFilename(tray, index) {
  const source = tray.stl || tray.stl_url || tray.url || '';
  const fromUrl = source.split('/').pop()?.split('?')[0];
  return fromUrl?.toLowerCase().endsWith('.stl') ? fromUrl : `${String(index + 1).padStart(2, '0')}_${tray.name.replace(/[^a-z0-9]+/gi, '_').replace(/^_|_$/g, '')}.stl`;
}
$('#download-all').addEventListener('click', async (event) => {
  event.preventDefault();
  const button = event.currentTarget; const originalLabel = button.innerHTML; button.textContent = 'Packing STL files…'; button.style.pointerEvents = 'none';
  try {
    const trays = await trayData(); const entries = [];
    for (const [index, tray] of trays.entries()) {
      const url = tray.stl_url || tray.url || `${ASSET_ROOT}${tray.stl}`; const response = await fetch(url);
      if (!response.ok) throw new Error(`Could not fetch ${tray.name}`);
      entries.push({ name: zipFilename(tray, index), bytes: new Uint8Array(await response.arrayBuffer()) });
    }
    const href = URL.createObjectURL(createStoredZip(entries)); const link = document.createElement('a');
    const game = $('#game-name').value.trim().replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '') || 'organizer';
    link.href = href; link.download = `${game}-stl-files.zip`; link.click(); setTimeout(() => URL.revokeObjectURL(href), 60000);
  } catch (error) { alert('Could not package every STL file. Confirm that all STL URLs are available to the browser.'); }
  finally { button.innerHTML = originalLabel; button.style.pointerEvents = ''; }
});
async function setupViewer() {
  const trays = await trayData(); renderFileList(trays);
  if (viewerReady) { if (scene) showTrays(trays); return; }
  viewerReady = true;
  try {
    const modules = await Promise.all([
      import('three'),
      import('three/addons/controls/OrbitControls.js'),
      import('three/addons/loaders/STLLoader.js')
    ]);
    THREE = modules[0]; ({ OrbitControls } = modules[1]); ({ STLLoader } = modules[2]);
  } catch (error) {
    $('#viewer').textContent = '3D preview is unavailable offline. Download the STL files to inspect them in your slicer.';
    return;
  }
  const mount = $('#viewer'); scene = new THREE.Scene(); scene.background = new THREE.Color('#10172b');
  camera = new THREE.PerspectiveCamera(38, mount.clientWidth / mount.clientHeight, .1, 1000); camera.position.set(180, 175, 240);
  renderer = new THREE.WebGLRenderer({ antialias: true }); renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); renderer.setSize(mount.clientWidth, mount.clientHeight); mount.append(renderer.domElement);
  controls = new OrbitControls(camera, renderer.domElement); controls.target.set(0, 0, 0); controls.enableDamping = true; controls.minDistance = 70; controls.maxDistance = 500;
  scene.add(new THREE.HemisphereLight('#f3f1ff', '#38435b', 2.8)); const light = new THREE.DirectionalLight('#ffffff', 2.8); light.position.set(100, 140, 90); scene.add(light);
  showTrays(trays);
  window.addEventListener('resize', resizeViewer); $('#reset-view').addEventListener('click', () => { camera.position.set(180, 175, 240); controls.target.set(0, 0, 0); });
  (function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); })();
}
// Replaces the previous organizer's meshes, so "Start another game" shows the new job's trays.
let viewerGeneration = 0; let viewerGrid = null;
function showTrays(trays) {
  const generation = ++viewerGeneration;
  trayMeshes.forEach((mesh) => { if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); mesh.material.dispose(); } });
  trayMeshes = []; if (viewerGrid) scene.remove(viewerGrid);
  const box = completedLayout?.box_inner_mm || activeLayout?.box_inner_mm || { width: 210, length: 210 };
  viewerGrid = new THREE.GridHelper(Math.max(box.width, box.length) + 50, 13, '#4d5a75', '#263149'); scene.add(viewerGrid);
  const loader = new STLLoader(); const colors = ['#a6f2cf','#b3a8ff','#ffb26b','#7bdff2','#f5a4bc','#fae68d','#b9c3da'];
  trays.forEach((tray, index) => loader.load(tray.stl_url || `${ASSET_ROOT}${tray.stl}`, (geometry) => {
    if (generation !== viewerGeneration) { geometry.dispose(); return; }   // a newer organizer replaced this one
    geometry.center(); geometry.rotateX(-Math.PI / 2);
    const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color: colors[index % colors.length], roughness: .55, metalness: .04 }));
    const outer = tray.outer_mm || { width: 55, length: 55, height: 25 };
    const footprint = tray.footprint_in_box_mm || { width: outer.width, length: outer.length };
    const pos = tray.position_mm || { x: (index % 3) * 65, y: Math.floor(index / 3) * 65 };
    // STL files are z-up. After rotateX, y is vertical and the backend's y becomes -z.
    // position_mm.z is the tray's base height: > 0 for trays stacked on other trays.
    mesh.rotation.y = THREE.MathUtils.degToRad(tray.rotation_degrees || 0);
    mesh.position.set(pos.x + footprint.width / 2 - box.width / 2, (pos.z || 0) + outer.height / 2, -(pos.y + footprint.length / 2 - box.length / 2));
    mesh.name = tray.name; scene.add(mesh); trayMeshes[index] = mesh;
  }));
}
function resizeViewer() { const mount = $('#viewer'); if (!renderer || !mount) return; camera.aspect = mount.clientWidth / mount.clientHeight; camera.updateProjectionMatrix(); renderer.setSize(mount.clientWidth, mount.clientHeight); }
function renderFileList(trays) {
  const list = $('#file-list'); list.innerHTML = '';
  trays.forEach((tray, index) => { const row = document.createElement('button'); row.className = `file-item ${index === 0 ? 'selected' : ''}`; const stlUrl = tray.stl_url || `${ASSET_ROOT}${tray.stl}`; row.innerHTML = `<span class="file-index">${String(index + 1).padStart(2, '0')}</span><span class="file-name">${tray.name}</span><span class="file-size">STL</span><a class="file-download" href="${stlUrl}" download aria-label="Download ${tray.name}">↓</a>`;
    row.addEventListener('click', (event) => { if (event.target.closest('a')) return; document.querySelectorAll('.file-item').forEach((item) => item.classList.remove('selected')); row.classList.add('selected'); focusTray(index); }); list.append(row);
  });
}
function focusTray(index) { const mesh = trayMeshes[index]; if (!mesh || !camera) return; currentTray = mesh; controls.target.copy(mesh.position); camera.position.copy(mesh.position).add(new THREE.Vector3(100, 105, 130)); controls.update(); }

