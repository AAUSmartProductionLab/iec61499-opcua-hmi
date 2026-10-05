'use strict';

const SESSION_KEY = 'opcua-hmi-session';
const MODULE_KEY = 'opcua-hmi-module';
const THEME_KEY = 'opcua-hmi-theme';

const HINTS = {
  Succeeded: 'Goal reached; back to Idle by itself in a moment. Start runs it again.',
  Failed: 'Start runs it again; Reset applies after Abort.',
  Aborted: 'Aborted. Reset (or module Clear) brings it back to Idle.',
  Running: 'Running. Stop ends it gently, Abort drops the outputs.',
  Stopping: 'Running the stop sequence.',
};

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem(THEME_KEY, theme);
  const button = document.getElementById('theme-toggle');
  if (button) button.textContent = theme === 'dark' ? 'light' : 'dark';
}

const state = {
  config: null,
  ui: {},
  active: null,
  logClearedAt: 0,
  logShown: '',
};

function childList(children) {
  if (children === null || children === undefined || children === false) return [];
  if (Array.isArray(children)) return children;
  if (typeof children === 'object') return Array.from(children);
  return [children];
}

const el = (tag, props = {}, children = []) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of childList(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === 'object' ? child : document.createTextNode(String(child)));
  }
  return node;
};

function sessionId() {
  let value = localStorage.getItem(SESSION_KEY);
  if (!value) {
    const random = (crypto && crypto.randomUUID) ? crypto.randomUUID()
      : 'sess-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10);
    value = random;
    localStorage.setItem(SESSION_KEY, value);
  }
  return value;
}

function toast(message, bad) {
  const box = document.getElementById('toast');
  box.textContent = message;
  box.className = bad ? 'toast bad' : 'toast';
  box.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { box.hidden = true; }, 4000);
}

async function api(path, body) {
  const options = {
    method: body ? 'POST' : 'GET',
    headers: { 'X-Session-Id': sessionId() },
  };
  if (body) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    toast(payload.error || `HTTP ${response.status}`, true);
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function badge(initialCss) {
  const node = el('span', { class: `badge ${initialCss || 'idle'}`, text: 'unknown' });
  node.update = (css, text) => {
    if (node.textContent !== text) node.textContent = text;
    if (node.className !== `badge ${css}`) node.className = `badge ${css}`;
  };
  return node;
}

function paramRow(param, onInput) {
  const input = el('input', {
    type: 'number', step: param.step, min: param.minimum, max: param.maximum,
    value: param.default, 'data-param': param.name,
  });
  input.addEventListener('input', () => {
    const value = Number(input.value);
    const bad = input.value === '' || Number.isNaN(value) || value < param.minimum || value > param.maximum;
    input.classList.toggle('bad', bad);
    if (onInput) onInput(param.name, value, bad);
  });
  return el('div', { class: 'param' }, [
    el('label', { text: param.unit ? `${param.name} (${param.unit})` : param.name }),
    el('div', { class: 'field' }, [
      input,
      el('span', { class: 'range', text: `${param.minimum}..${param.maximum}` }),
    ]),
  ]);
}

function stepsList(steps) {
  const items = steps.map((step, index) => {
    const badgeNode = badge('idle');
    const values = el('span', { class: 'pvals' });
    const item = {
      name: step.name,
      node: el('li', {}, [
        el('span', { class: 'num', text: String(index + 1) }),
        el('span', {}, [step.label, values]),
        badgeNode,
      ]),
      badge: badgeNode,
      values,
      errorText: '',
    };
    return item;
  });
  return {
    nodes: items,
    node: el('ol', { class: 'steps' }, items.map((item) => item.node)),
    update(steps) {
      steps.forEach((step, index) => {
        const item = items[index];
        if (!item) return;
        item.badge.update(step.stateCss, step.stateName);
        item.node.classList.toggle('active', step.state === 1);
        item.node.classList.toggle('failed', step.state === 4);
        const pairs = Object.entries(step.params || {})
          .filter(([, value]) => value !== null && value !== undefined)
          .map(([name, value]) => `${name}=${Number(value).toFixed(1)}`);
        const text = step.errorId ? `${pairs.join(' ')} - ${step.errorText}` : pairs.join(' ');
        if (item.values.textContent !== text) item.values.textContent = text;
      });
    },
  };
}

function buildSkillCard(moduleKey, skill, send) {
  const stateBadge = badge('idle');
  const errorLine = el('p', { class: 'err-line' });
  const paramsBox = el('div', { class: 'params' });
  const inputs = new Map();
  for (const param of skill.params) {
    const row = paramRow(param, (name, value, bad) => {
      if (bad) window.HmiDiagrams.setCommands(miniDiagram, { Start: false }, 'a parameter is out of range');
    });
    paramsBox.appendChild(row);
    inputs.set(param.name, row.querySelector('input'));
  }
  const resultsBox = el('div', { class: 'results' });
  const steps = stepsList(skill.steps);
  const stopSteps = skill.stopSteps.length ? stepsList(skill.stopSteps) : null;
  const miniDiagram = window.HmiDiagrams.miniSkillSvg(state.config, null);
  window.HmiDiagrams.bindCommands(miniDiagram, (command) => {
    const params = {};
    for (const [name, input] of inputs) params[name] = Number(input.value);
    send({ module: moduleKey, skill: skill.name, command, params });
  });
  const uses = el('div', { class: 'uses' });
  const hint = el('p', { class: 'skill-hint' });
  const caption = el('p', { class: 'skill-caption' });
  const card = {
    node: el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [
        el('div', { class: 'title' }, [
          el('h3', { text: skill.label }),
          el('small', { text: skill.moduleLevel ? 'module level skill' : skill.name }),
          caption,
        ]),
        stateBadge,
      ]),
      el('p', { class: 'desc', text: skill.description }),
      miniDiagram,
      paramsBox,
      resultsBox,
      errorLine,
      skill.steps.length ? el('div', { class: 'section-title' }, [
        el('h2', { text: 'Execute' }), el('span', { text: 'steps in order' }),
      ]) : null,
      skill.steps.length ? steps.node : null,
      stopSteps ? el('div', { class: 'section-title' }, [
        el('h2', { text: 'Stopping' }), el('span', { text: 'stop sequence' }),
      ]) : null,
      stopSteps ? stopSteps.node : null,
      uses,
      hint,
    ]),
    stateBadge,
    errorLine,
    hint,
    caption,
    uses,
    usesText: skill.uses.join(', '),
    inputs,
    steps,
    stopSteps,
    miniDiagram,
    results: resultsBox,
    resultNodes: new Map(),
  };
  return card;
}

function buildModule(profile) {
  const send = (payload) => api('/api/skill-command', payload)
    .then((result) => {
      if (result.ok && !result.accepted) toast(`${result.errorText}`, true);
    })
    .catch(() => {});

  const occupationDiagram = window.HmiDiagrams.occupationSvg(null);
  window.HmiDiagrams.bindCommands(occupationDiagram, (command) => {
    api('/api/occupation', { module: profile.key, action: command.toLowerCase() })
      .then((result) => {
        if (result.ok && !result.accepted) toast(`${result.errorText}`, true);
      })
      .catch(() => {});
  });

  const moduleDiagram = window.HmiDiagrams.moduleSvg(state.config, null);

  window.HmiDiagrams.bindCommands(moduleDiagram, (command) => {
    api('/api/module-command', { module: profile.key, command })
      .then((result) => {
        if (result.ok && !result.accepted) toast(`${result.errorText}`, true);
      })
      .catch(() => {});
  });

  const sensorBox = el('div', { class: 'sensors' });
  const sensorNodes = new Map();
  for (const sensor of profile.sensors) {
    const value = el('div', { class: 'value off', text: '-' });
    sensorNodes.set(sensor.name, value);
    sensorBox.appendChild(el('div', { class: 'sensor' }, [
      el('div', { class: 'label', text: sensor.label }),
      value,
      el('div', { class: 'label', text: `${sensor.equipment}${sensor.unit ? ' / ' + sensor.unit : ''}` }),
    ]));
  }

  const skillBox = el('div', { class: 'grid' });
  const sequenceBox = el('div', { class: 'grid' });
  const cards = new Map();
  for (const skill of profile.skills) {
    const card = buildSkillCard(profile.key, skill, send);
    cards.set(skill.name, card);
    (skill.moduleLevel ? sequenceBox : skillBox).appendChild(card.node);
  }
  const hasSequences = sequenceBox.children.length > 0;

  const procedureBox = el('div', { class: 'grid' });
  const procedureViews = new Map();
  for (const procedure of profile.procedures) {
    const steps = stepsList(procedure.steps);
    const body = el('div', { class: 'card' }, [
      el('div', { class: 'card-head' }, [el('h3', { text: procedure.label })]),
      steps.node,
    ]);
    procedureBox.appendChild(body);
    procedureViews.set(procedure.name, steps);
  }

  const alertLine = el('p', { class: 'module-alert', hidden: true });
  const diagramHint = el('span', { class: 'hint' });
  const node = el('section', { class: 'module', id: `module-${profile.key}` }, [
    el('div', { class: 'module-head' }, [
      el('h2', { text: profile.title }),
      el('span', { class: 'endpoint', text: profile.summary }),
    ]),
    alertLine,
    el('div', { class: 'panel occupation-panel' }, [
      el('div', { class: 'panel-head' }, [
        el('h2', { text: 'Occupation' }),
        el('span', { class: 'hint', text: 'only the session that occupies the module commands it' }),
      ]),
      occupationDiagram,
    ]),
    el('div', { class: 'panel diagram-panel' }, [
      el('div', { class: 'panel-head' }, [el('h2', { text: 'State machine' }), diagramHint]),
      moduleDiagram,
    ]),
    hasSequences
      ? el('div', { class: 'panel' }, [
        el('div', { class: 'panel-head' }, [
          el('h2', { text: 'Sequences' }),
          el('span', { class: 'hint', text: 'module level skills: several steps in order' }),
        ]),
        sequenceBox,
      ])
      : null,
    el('div', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [el('h2', { text: 'Equipment' })]),
      sensorBox,
    ]),
    el('div', { class: 'panel' }, [
      el('div', { class: 'panel-head' }, [
        el('h2', { text: 'Skills' }),
        el('span', { class: 'hint', text: 'single motions and operations' }),
      ]),
      skillBox,
    ]),
    procedureBox.children.length
      ? el('div', { class: 'panel' }, [
        el('div', { class: 'panel-head' }, [el('h2', { text: 'Procedures' })]),
        procedureBox,
      ])
      : null,
    profile.notes.length || profile.equipmentNotes.length
      ? el('div', { class: 'panel' }, [
        el('div', { class: 'panel-head' }, [el('h2', { text: 'Notes' })]),
        el('ul', { class: 'notes' }, [
          ...profile.notes.map((note) => el('li', { text: note })),
          ...profile.equipmentNotes.map((note) => el('li', { text: note })),
        ]),
      ])
      : null,
  ]);


  return {
    node,
    occupationDiagram,
    alertLine,
    diagramHint,
    moduleDiagram,
    sensorNodes,
    cards,
    procedureViews,
    moduleKey: profile.key,
  };
}

function renderConfig(config) {
  const tabs = document.getElementById('tabs');
  const main = document.getElementById('modules');
  tabs.textContent = '';
  main.textContent = '';
  state.ui = {};
  for (const profile of config.modules) {
    const ui = buildModule(profile);
    state.ui[profile.key] = ui;
    main.appendChild(ui.node);
    tabs.appendChild(el('button', {
      type: 'button', class: '', text: profile.title,
      onclick: () => selectModule(profile.key),
    }));
    ui.tab = tabs.lastChild;
  }
  const stored = localStorage.getItem(MODULE_KEY);
  selectModule(state.ui[stored] ? stored : config.modules[0].key);
}

function selectModule(key) {
  if (!state.ui[key]) return;
  state.active = key;
  localStorage.setItem(MODULE_KEY, key);
  for (const [moduleKey, ui] of Object.entries(state.ui)) {
    const active = moduleKey === key;
    ui.node.hidden = !active;
    ui.tab.classList.toggle('active', active);
  }
}

function updateSkill(ui, module, skillName, skill) {
  const card = ui.cards.get(skillName);
  if (!card) return;
  const link = ui.links.get(skillName);
  const shown = link ? link.step : skill;
  card.stateBadge.update(shown.stateCss, shown.stateName);
  window.HmiDiagrams.setActive(card.miniDiagram, shown.state);
  card.errorLine.textContent = shown.errorId ? shown.errorText : '';
  card.node.classList.toggle('linked', Boolean(link));
  const caption = link ? `running as step ${link.index} of ${link.parent}` : '';
  if (card.caption.textContent !== caption) card.caption.textContent = caption;
  const hint = link ? HINTS[shown.stateName] || '' : (HINTS[skill.stateName] || '');
  if (card.hint.textContent !== hint) card.hint.textContent = hint;
  const badInput = Array.from(card.inputs.values()).some((input) => input.classList.contains('bad'));
  const commands = { ...skill.commands, Start: skill.commands.Start && !link && !badInput };
  let why = 'not possible in this state';
  if (!module.occupier) why = 'occupy the module first';
  else if (badInput) why = 'a parameter is out of range';
  else if (link) why = `it runs as a step of ${link.parent}`;
  window.HmiDiagrams.setCommands(card.miniDiagram, commands, why);
  const held = skill.heldBy && skill.heldBy.length ? ` - held by ${skill.heldBy.join(', ')}` : '';
  const usesText = card.usesText ? `uses: ${card.usesText}${held}` : '';
  if (card.uses.textContent !== usesText) card.uses.textContent = usesText;
  card.uses.classList.toggle('held', Boolean(held));
  card.steps.update(skill.steps);
  if (card.stopSteps) card.stopSteps.update(skill.stopSteps);
  const wanted = new Set();
  for (const [name, result] of Object.entries(skill.results)) {
    wanted.add(name);
    let node = card.resultNodes.get(name);
    if (!node) {
      node = el('span', {}, [el('span', { text: `${name}: ` }), el('b', { text: '-' })]);
      card.resultNodes.set(name, node);
      card.results.appendChild(node);
    }
    const value = node.querySelector('b');
    const text = result.value === null || result.value === undefined
      ? '-' : `${Number(result.value).toFixed(2)}${result.unit ? ' ' + result.unit : ''}`;
    if (value.textContent !== text) value.textContent = text;
  }
  for (const [name, node] of card.resultNodes) {
    if (!wanted.has(name)) {
      node.remove();
      card.resultNodes.delete(name);
    }
  }
}

/**
 * A composite (module level) skill runs private instances of its skill
 * primitives and does not touch the standalone skill variables, only its own
 * step variables. So while a composite runs (or runs its stop sequence), the
 * primitive that the current step runs (ArmIn runs MoveArm) shows the state of
 * that step, straight from the controller's subscription.
 */
function activeSteps(module) {
  const links = new Map();
  for (const [name, skill] of Object.entries(module.skills)) {
    if (!skill.moduleLevel || (skill.stateName !== 'Running' && skill.stateName !== 'Stopping')) continue;
    const groups = [['', skill.steps], ['stop ', skill.stopSteps || []]];
    for (const [prefix, steps] of groups) {
      for (const step of steps) {
        if (step.stateName !== 'Running' && step.stateName !== 'Stopping') continue;
        const primitive = step.skill || step.name;
        if (module.skills[primitive]) {
          links.set(primitive, { parent: name, index: `${prefix}${step.index}`, step });
        }
      }
    }
  }
  return links;
}

function updateModule(ui, module) {
  window.HmiDiagrams.setActive(ui.moduleDiagram, module.moduleState.value);
  window.HmiDiagrams.setCommands(
    ui.moduleDiagram, module.commands,
    module.occupier ? 'not possible in this state' : 'occupy the module first',
  );
  const hint = module.occupier ? 'press a command on its line' : 'occupy the module to command it';
  if (ui.diagramHint.textContent !== hint) ui.diagramHint.textContent = hint;

  let occupation = 0;
  if (module.occupier) occupation = 1;
  else if (module.occupied) occupation = 2;
  window.HmiDiagrams.setActive(ui.occupationDiagram, occupation);
  window.HmiDiagrams.setCommands(ui.occupationDiagram, {
    Occupy: module.occupation.occupy && !module.occupied,
    Release: module.occupation.release,
  }, module.occupied && !module.occupier ? 'another session occupies the module' : 'not possible now');

  // Only what is wrong with the connection is shown; the drawings show the rest.
  let alert = '';
  if (module.missing.length) alert = `The module's address space is incomplete, missing: ${module.missing.join(', ')}`;
  else if (!module.connected) alert = `Not connected to ${module.endpoint}${module.detail ? ` (${module.detail})` : ''}; the values shown are stale.`;
  if (ui.alertLine.textContent !== alert) ui.alertLine.textContent = alert;
  ui.alertLine.hidden = !alert;

  for (const [name, valueNode] of ui.sensorNodes) {
    const sensor = module.sensors[name];
    if (!sensor) continue;
    let text = '-';
    let css = 'off';
    if (sensor.kind === 'bool') {
      text = sensor.value ? 'true' : 'false';
      css = sensor.value ? 'on' : 'off';
    } else if (sensor.value !== null && sensor.value !== undefined) {
      text = `${Number(sensor.value).toFixed(2)}${sensor.unit ? ' ' + sensor.unit : ''}`;
      css = 'on';
    }
    if (sensor.status !== 'Good') text = `${text} (${sensor.status})`;
    if (valueNode.textContent !== text) valueNode.textContent = text;
    if (valueNode.className !== `value ${css}`) valueNode.className = `value ${css}`;
  }

  ui.links = activeSteps(module);
  for (const [name, skill] of Object.entries(module.skills)) updateSkill(ui, module, name, skill);
  for (const [name, procedure] of Object.entries(module.procedures)) {
    const view = ui.procedureViews.get(name);
    if (view) view.update(procedure.steps);
  }
}

function renderLog(entries) {
  const box = document.getElementById('log');
  const shown = entries.filter((entry) => entry.ts > state.logClearedAt).slice(-120);
  const key = shown.length ? `${shown.length}:${shown[shown.length - 1].ts}` : '';
  if (key === state.logShown) return;
  state.logShown = key;
  // Follow new entries only while the operator is at the end of the log.
  const atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  box.textContent = '';
  for (const entry of shown) {
    box.appendChild(el('li', {}, [
      el('span', { class: 't', text: entry.time }),
      el('span', { class: 'm', text: entry.module }),
      el('span', { class: entry.level, text: entry.text }),
    ]));
  }
  if (atEnd) box.scrollTop = box.scrollHeight;
}

function showSnapshot(snapshot) {
  for (const [key, module] of Object.entries(snapshot.modules)) {
    const ui = state.ui[key];
    if (ui) updateModule(ui, module);
  }
  renderLog(snapshot.log);
}

async function poll() {
  // Only while the push channel is down, and one loop at a time.
  if (state.socketOpen || state.polling) return;
  state.polling = true;
  try {
    showSnapshot(await api(`/api/snapshot?session=${encodeURIComponent(sessionId())}`));
  } catch (error) {
    if (!String(error.message).includes('missing session')) toast(error.message, true);
  } finally {
    state.polling = false;
    if (!state.socketOpen) setTimeout(poll, 250);
  }
}

function listen() {
  // The service pushes a snapshot whenever something changes; polling covers the gaps.
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const url = `${scheme}://${window.location.host}/ws?session=${encodeURIComponent(sessionId())}`;
  let socket;
  try {
    socket = new WebSocket(url);
  } catch (error) {
    setTimeout(listen, 2000);
    return;
  }
  socket.addEventListener('open', () => { state.socketOpen = true; });
  socket.addEventListener('message', (event) => {
    try { showSnapshot(JSON.parse(event.data)); } catch (error) { console.error(error); }
  });
  socket.addEventListener('close', () => {
    const wasOpen = state.socketOpen;
    state.socketOpen = false;
    if (wasOpen) poll();
    setTimeout(listen, 2000);
  });
}

async function boot() {
  const forced = new URLSearchParams(window.location.search).get('theme');
  const stored = localStorage.getItem(THEME_KEY);
  applyTheme(forced || stored || 'light');
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    applyTheme(current === 'dark' ? 'light' : 'dark');
  });
  document.getElementById('session-id').textContent = sessionId();
  state.config = await api('/api/config');
  renderConfig(state.config);
  document.getElementById('clear-log').addEventListener('click', () => {
    // The log lives in the HMI service; hide what is there now, keep what comes.
    state.logClearedAt = Date.now() / 1000;
    state.logShown = null;
    document.getElementById('log').textContent = '';
  });
  poll();
  listen();
}

boot().catch((error) => toast(error.message, true));