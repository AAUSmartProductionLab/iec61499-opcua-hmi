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

// Every drawing has its own arrowhead: a url(#id) resolves to the first element
// with that id, and the first drawing may sit in a hidden module tab.
let markerCount = 0;

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

function defs(root, size = 7) {
  markerCount += 1;
  const id = `hmi-arrow-${markerCount}`;
  root.setAttribute('data-marker', id);
  const node = svgEl('defs');
  const marker = svgEl('marker', {
    id, viewBox: '0 0 10 10', refX: 9, refY: 5,
    markerWidth: size, markerHeight: size, orient: 'auto-start-reverse',
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

const MARKER_RADIUS = 3.5;

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
    dur: '0.9s', begin: 'indefinite', path: route, fill: 'remove', rotate: 'auto', calcMode: 'linear',
  });
  dot.appendChild(motion);
  root.appendChild(dot);
  // The animation runs on the drawing's own timeline, which started at load:
  // start it now, and take the dot away when it has arrived.
  motion.addEventListener('endEvent', () => dot.remove());
  setTimeout(() => dot.remove(), 1500);
  motion.beginElement();
}

function commandBox(value, x, y) {
  const width = Math.max(40, value.length * 6.8 + 14);
  // A command on the drawing is its button; setCommands enables it.
  const group = svgEl('g', {
    class: 'command disabled', 'data-command': value, role: 'button',
    'aria-label': value, 'aria-disabled': 'true', tabindex: '-1',
  });
  const title = svgEl('title');
  title.textContent = value;
  group.appendChild(title);
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

function edge(path, label, style, x, y, marker) {
  const group = svgEl('g', { class: `edge ${style}` });
  group.appendChild(svgEl('path', {
    d: path, class: 'edge-line', 'marker-end': `url(#${marker})`,
  }));
  if (style === 'command') {
    group.appendChild(commandBox(label, x, y));
  } else if (label) {
    group.appendChild(noteBox(label, x, y));
  }
  return { group, path };
}

/*
 * Shared drawing of a state machine: dashed frames for the states a command
 * applies to, the edges, then the state boxes on top. An edge is
 * [from, to, path, label, labelX, labelY]; from is a state code or the name of
 * a frame (the command leaves every state in it); a labelled edge is a command,
 * an unlabelled one is state complete.
 */
function drawMachine(root, config, machine) {
  for (const frame of machine.frames) {
    root.appendChild(svgEl('rect', {
      x: frame.x, y: frame.y, width: frame.w, height: frame.h, rx: 10,
      class: 'diagram-frame superstate',
    }));
    if (frame.label) {
      root.appendChild(svgText(frame.label, {
        x: frame.x + 12, y: frame.y + 15, class: 'superstate-label',
      }));
    }
  }
  for (const [, into, path, label, x, y] of machine.edges) {
    root.appendChild(edge(path, label, label ? 'command' : 'sc', x, y, root.getAttribute('data-marker')).group);
    if (!root.getAttribute(`data-into-${into}`)) root.setAttribute(`data-into-${into}`, path);
  }
  for (const [code, [x, y]] of Object.entries(machine.positions)) {
    root.appendChild(stateBox(
      { name: nameOf(config.states, code, code), kind: kindOf(config.kinds, code, 'waits') },
      x, y, machine.box.w, machine.box.h, false, code,
    ));
  }
}

/*
 * The module machine (MOD_StateLogic). Row one runs from Stopped to Execute,
 * Stopping and the abort row return to Stopped along their own lanes. Stop is
 * accepted in the inner frame, Abort in the outer one; Starting sits inside
 * both but passes at once, so neither applies to it.
 */
const MODULE = {
  box: { w: 124, h: 46 },
  positions: {
    2: [40, 84],     // Stopped
    15: [260, 84],   // Resetting
    4: [480, 84],    // Idle
    3: [700, 84],    // Starting
    6: [920, 84],    // Execute
    7: [920, 190],   // Stopping
    8: [700, 290],   // Aborting
    9: [480, 290],   // Aborted
    1: [260, 290],   // Clearing
  },
  frames: [
    { name: 'abort', label: 'ABORT', x: 16, y: 30, w: 1058, h: 222, members: [2, 15, 4, 6, 7] },
    { name: 'stop', label: 'STOP', x: 236, y: 58, w: 824, h: 86, members: [15, 4, 6] },
  ],
  edges: [
    [2, 15, 'M164,107 H260', 'Reset', 200, 107],
    [15, 4, 'M384,107 H480', '', 0, 0],
    [4, 3, 'M604,107 H700', 'Start', 652, 107],
    [3, 6, 'M824,107 H920', '', 0, 0],
    ['stop', 7, 'M982,144 V190', 'Stop', 982, 167],
    [7, 2, 'M920,213 H124 V130', '', 0, 0],
    ['abort', 8, 'M762,252 V290', 'Abort', 762, 271],
    [8, 9, 'M700,313 H604', '', 0, 0],
    [9, 1, 'M480,313 H384', 'Clear', 432, 313],
    [1, 2, 'M260,313 H80 V130', '', 0, 0],
  ],
};

function moduleSvg(config, current) {
  const root = svgEl('svg', {
    viewBox: '0 0 1090 372', class: 'diagram module-diagram',
    preserveAspectRatio: 'xMidYMid meet',
  });
  root.appendChild(defs(root));
  root.appendChild(svgText('Module state machine (PackML)', { x: 16, y: 18, class: 'frame-label' }));
  drawMachine(root, { states: config.moduleStates, kinds: config.moduleStateKinds }, MODULE);
  root.appendChild(svgText('also after a failed procedure or the stop timeout', {
    x: 790, y: 275, class: 'frame-note',
  }));
  root.appendChild(svgText(
    'Stop is accepted in the inner frame, Abort in the outer one. '
    + 'Starting and Clearing pass at once; unlabelled lines are state complete.',
    { x: 16, y: 362, class: 'frame-note' },
  ));
  setActive(root, current);
  return root;
}

/*
 * The skill machine of the controller (SKILL_Control). Start is accepted in
 * Idle, Succeeded and Failed (the inner frame); Stop passes Stopping and ends
 * in Failed with ErrorID 7; Abort leaves every state in the outer frame; Reset
 * (or the module in Clearing or Stopped) brings an aborted skill back to Idle.
 * Nothing else returns to Idle.
 */
const SKILL = {
  box: { w: 84, h: 30 },
  positions: {
    0: [40, 22],     // Idle
    3: [40, 96],     // Succeeded
    4: [40, 174],    // Failed
    1: [220, 96],    // Running
    2: [220, 174],   // Stopping
    5: [120, 266],   // Aborted
  },
  frames: [
    { name: 'abort', x: 22, y: 4, w: 300, h: 218, members: [0, 1, 2, 3, 4] },
    { name: 'start', x: 30, y: 12, w: 104, h: 202, members: [0, 3, 4] },
  ],
  edges: [
    ['start', 1, 'M134,37 H262 V96', 'Start', 198, 37],
    [1, 3, 'M220,104 H124', '', 0, 0],
    [1, 4, 'M220,118 H178 V182 H124', '', 0, 0],
    [1, 2, 'M262,126 V174', 'Stop', 262, 150],
    [2, 4, 'M220,196 H124', '', 0, 0],
    ['abort', 5, 'M162,222 V266', 'Abort', 162, 244],
    [5, 0, 'M120,281 H10 V37 H40', 'Reset', 64, 281],
  ],
};

function miniSkillSvg(config, current) {
  const root = svgEl('svg', {
    viewBox: '0 0 330 300', class: 'diagram mini-skill-diagram',
    preserveAspectRatio: 'xMidYMid meet',
  });
  root.appendChild(defs(root, 4));
  drawMachine(root, { states: config.skillStates, kinds: config.skillStateKinds }, SKILL);
  setActive(root, current);
  return root;
}

/* Pressing a command on a drawing: onPress(command) for an enabled one. */
function bindCommands(root, onPress) {
  const press = (target) => {
    const group = target && target.closest ? target.closest('.command') : null;
    if (!group || !root.contains(group) || group.getAttribute('aria-disabled') !== 'false') return;
    onPress(group.getAttribute('data-command'));
  };
  root.addEventListener('click', (event) => press(event.target));
  root.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    press(event.target);
  });
}

/* Enable or disable the commands named in `enabled` ({Start: true, ...}). */
function setCommands(root, enabled, why) {
  for (const group of root.querySelectorAll('.command[data-command]')) {
    const command = group.getAttribute('data-command');
    if (!(command in enabled)) continue;
    const on = Boolean(enabled[command]);
    const title = on ? command : `${command}: ${why || 'not possible in this state'}`;
    const label = group.querySelector('title');
    if (label.textContent !== title) label.textContent = title;
    if (group.getAttribute('aria-disabled') === String(!on)) continue;
    group.setAttribute('aria-disabled', String(!on));
    group.setAttribute('tabindex', on ? '0' : '-1');
    group.classList.toggle('enabled', on);
    group.classList.toggle('disabled', !on);
  }
}

window.HmiDiagrams = { moduleSvg, miniSkillSvg, setActive, bindCommands, setCommands };