'use strict';

const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs) {
  const node = document.createElementNS(NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  return node;
}

function svgText(value, attrs) {
  const node = svgEl('text', attrs);
  node.textContent = value;
  return node;
}

const MARKER_ID = 'hmi-arrow';

function nameOf(table, code, fallback) {
  const value = table[String(code)];
  if (value === undefined) return String(code);
  return (value && typeof value === 'object') ? value.name : value;
}

function kindOf(table, code, fallback) {
  const value = table[String(code)];
  if (value === undefined || value === null) return fallback;
  if (typeof value === 'object') return value.kind || fallback;
  return String(value);
}

function defs() {
  const node = svgEl('defs');
  const marker = svgEl('marker', {
    id: MARKER_ID, viewBox: '0 0 10 10', refX: 9, refY: 5,
    markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse',
  });
  marker.appendChild(svgEl('path', { d: 'M0,0 L10,5 L0,10 z', class: 'arrowhead' }));
  node.appendChild(marker);
  return node;
}

function stateBox(spec, x, y, width, height, active, code) {
  const group = svgEl('g', {
    class: `state ${spec.kind}${active ? ' active' : ''}`, 'data-code': code,
  });
  group.appendChild(svgEl('rect', {
    x, y, width, height, rx: 9, class: 'state-box',
  }));
  group.appendChild(svgText(spec.name, {
    x: x + width / 2, y: y + height / 2 + 1, 'text-anchor': 'middle',
    'dominant-baseline': 'middle', class: 'state-name',
  }));
  return group;
}

const MARKER_RADIUS = 5;

/**
 * Show the current state without painting over it: the box of the current state
 * breathes, and a dot travels along the transition that just completed, so the
 * operator sees how the state was reached.
 */
function setActive(root, code) {
  if (code === null || code === undefined) return;
  const previous = root.getAttribute('data-current');
  if (String(code) === previous) return;
  root.setAttribute('data-current', code);
  for (const group of root.querySelectorAll('.state')) {
    const isActive = String(code) === group.getAttribute('data-code');
    const kind = group.getAttribute('class').includes('acts') ? 'acts' : 'waits';
    group.setAttribute('class', `state ${kind}${isActive ? ' active' : ''}`);
  }
  if (previous === null) return;
  const route = root.getAttribute(`data-into-${code}`);
  if (!route) return;
  const dot = svgEl('circle', { r: MARKER_RADIUS, class: 'state-dot' });
  const motion = svgEl('animateMotion', {
    dur: '0.9s', path: route, fill: 'remove', rotate: 'auto', calcMode: 'linear',
  });
  dot.appendChild(motion);
  root.appendChild(dot);
}

function commandBox(value, x, y) {
  const width = Math.max(40, value.length * 6.8 + 14);
  const group = svgEl('g', { class: 'command' });
  group.appendChild(svgEl('rect', {
    x: x - width / 2, y: y - 10, width, height: 20, rx: 10, class: 'command-box',
  }));
  group.appendChild(svgText(value, {
    x, y: y + 1, 'text-anchor': 'middle', 'dominant-baseline': 'middle',
    class: 'command-label',
  }));
  return group;
}

function noteBox(value, x, y) {
  const width = value.length * 6.2 + 12;
  const group = svgEl('g', { class: 'note' });
  group.appendChild(svgEl('rect', {
    x: x - width / 2, y: y - 9, width, height: 18, rx: 9, class: 'note-box',
  }));
  group.appendChild(svgText(value, {
    x, y: y + 1, 'text-anchor': 'middle', 'dominant-baseline': 'middle',
    class: 'note-label',
  }));
  return group;
}

function edge(path, label, style, x, y, into) {
  const group = svgEl('g', { class: `edge ${style}` });
  group.appendChild(svgEl('path', {
    d: path, class: 'edge-line', 'marker-end': `url(#${MARKER_ID})`,
  }));
  if (style === 'command') {
    group.appendChild(commandBox(label, x, y));
  } else if (label) {
    group.appendChild(noteBox(label, x, y));
  }
  return { group, path, into };
}

const MODULE_BOX = { w: 124, h: 46 };
const MODULE_POSITIONS = {
  2: [880, 150],   // Stopped
  15: [40, 54],    // Resetting
  3: [250, 150],   // Starting
  4: [40, 150],    // Idle
  6: [460, 150],   // Execute
  7: [670, 150],   // Stopping
  8: [460, 266],   // Aborting
  9: [670, 266],   // Aborted
  1: [880, 266],   // Clearing
};

function moduleSvg(config, current) {
  const root = svgEl('svg', {
    viewBox: '0 0 1120 348', class: 'diagram module-diagram',
    preserveAspectRatio: 'xMidYMid meet',
  });
  root.appendChild(defs());
  root.appendChild(svgEl('rect', {
    x: 8, y: 34, width: 1104, height: 176, rx: 12, class: 'diagram-frame',
  }));
  root.appendChild(svgText('Module state machine (PackML)', {
    x: 12, y: 16, class: 'frame-label',
  }));

  const states = config.moduleStates;
  const kinds = config.moduleStateKinds;
const edges = [
    ['M960,150 V24 H102 V54', 'Reset', 'command', 530, 24, 15],
    ['M102,100 V150', 'SC', 'sc', 122, 125, 4],
    ['M164,173 H250', 'Start', 'command', 207, 173, 3],
    ['M374,173 H460', 'SC', 'sc', 417, 158, 6],
    ['M584,173 H670', 'Stop', 'command', 627, 173, 7],
    ['M794,173 H880', 'SC', 'sc', 837, 158, 2],
    ['M522,196 V266', 'Abort', 'command', 522, 231, 8],
    ['M584,289 H670', 'SC', 'sc', 627, 272, 9],
    ['M794,289 H880', 'Clear', 'command', 837, 289, 1],
    ['M942,266 V196', 'SC', 'sc', 966, 231, 2],
  ];
  for (const [path, label, style, x, y, into] of edges) {
    const built = edge(path, label, style, x, y);
    root.appendChild(built.group);
    if (!root.getAttribute(`data-into-${into}`)) {
      root.setAttribute(`data-into-${into}`, path);
    }
  }
  for (const [code, [x, y]] of Object.entries(MODULE_POSITIONS)) {
    root.appendChild(stateBox(
      { name: nameOf(states, code, code), kind: kindOf(kinds, code, 'waits') },
      x, y, MODULE_BOX.w, MODULE_BOX.h,
      Number(code) === Number(current), code,
    ));
  }
  root.appendChild(svgText(
    'Stop: from Resetting, Idle, Execute (drawn from Execute).  '
    + 'Abort: from Stopped, Resetting, Idle, Execute, Stopping (drawn from Execute).',
    { x: 12, y: 332, class: 'frame-note' },
  ));
  return root;
}


/*
 * The reduced skill machine as the operator sees it: one action runs the skill,
 * and every end state falls back to Idle. Stopping is left out on purpose, it is
 * too short to see and the machine clears itself (see the simulator).
 */
const MINI_BOX = { w: 78, h: 26 };
const MINI_POSITIONS = {
  0: [4, 52],    // Idle
  1: [100, 52],  // Running
  3: [204, 8],   // Succeeded
  4: [100, 100], // Failed
  5: [196, 100], // Aborted
};

function miniSkillSvg(config, current) {
  const root = svgEl('svg', {
    viewBox: '0 0 292 150', class: 'diagram mini-skill-diagram',
    preserveAspectRatio: 'xMidYMid meet',
  });
  root.appendChild(defs());
  const states = config.skillStates;
  const kinds = config.skillStateKinds;
  const edges = [
    ['M82,65 H100', '', 'sc', 0, 0, 1],
    ['M139,59 L204,21', '', 'sc', 0, 0, 3],
    ['M139,78 V100', '', 'sc', 0, 0, 4],
    ['M160,78 H235 V100', '', 'sc', 0, 0, 5],
    ['M204,21 Q130,-10 43,52', '', 'sc', 0, 0, 0],
    ['M100,113 Q45,140 25,78', '', 'sc', 0, 0, 0],
    ['M196,113 Q110,158 55,78', '', 'sc', 0, 0, 0],
  ];
  for (const [path, label, style, x, y, into] of edges) {
    root.appendChild(edge(path, label, style, x, y).group);
    if (!root.getAttribute(`data-into-${into}`)) {
      root.setAttribute(`data-into-${into}`, path);
    }
  }
  for (const [code, [x, y]] of Object.entries(MINI_POSITIONS)) {
    root.appendChild(stateBox(
      { name: nameOf(states, code, code), kind: kindOf(kinds, code, 'waits') },
      x, y, MINI_BOX.w, MINI_BOX.h, Number(code) === Number(current), code,
    ));
  }
  return root;
}

window.HmiDiagrams = { moduleSvg, miniSkillSvg, setActive };