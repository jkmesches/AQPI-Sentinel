// What a key press means on the map console.
//
// Run:  node validation_tests/js/run_map_keys.mjs
//
// A global keydown listener on a page full of controls gets three things
// wrong, and most of these assertions are about them.
//
// It fires on a key the focused control handles itself. The console has a
// product picker, a time slider, opacity sliders and a row of buttons, and
// space, the arrows and the digits all mean something inside those. Space
// with the play button focused would activate the button and toggle playback,
// which cancel each other and read as a dead key.
//
// It stops firing on the keys that control does NOT handle. Taking the whole
// keyboard from any focused control is the obvious rule and it is wrong, since
// clicking play and then pressing an arrow is ordinary and a button never
// handled an arrow. Driving the deployed page found this: every check after
// the play button took focus failed.
//
// It swallows the browser's own shortcuts. Ctrl+R and Cmd+Left have to reach
// the browser whatever the map wanted the key for.
//
// It fights MapLibre for the arrows. MapLibre's keyboard handler pans with
// them whenever the canvas has focus, which it has as soon as anyone clicks
// the map to read a value. The console takes the bare arrows for time and
// gives the shifted ones to panning, so the caller can disable that handler
// without taking keyboard panning away.

const BOUND = [' ', 'k', 'j', 'l', 'c', '[', '?', 'Escape', '+', '=', '-',
  '_', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'];

const role = (r) => ({ tagName: 'DIV', getAttribute: (n) => (n === 'role' ? r : null) });
const input = (t) => ({ tagName: 'INPUT', getAttribute: (n) => (n === 'type' ? t : null) });

// Text entry owns every key, because any of them may be what is being typed.
const TEXT_TARGETS = [
  { tagName: 'TEXTAREA' },
  { tagName: 'INPUT' },
  input('text'),
  input('search'),
  { tagName: 'DIV', isContentEditable: true },
  role('textbox'),
  role('searchbox')
];

// The rest own only what they act on, listed per kind.
const KIND_TARGETS = {
  list: [{ tagName: 'SELECT' }, { tagName: 'OPTION' }, role('listbox'),
    role('combobox')],
  slider: [input('range'), role('slider'), role('spinbutton')],
  button: [{ tagName: 'BUTTON' }, input('checkbox'), input('submit'),
    role('button'), role('switch'), role('tab')]
};

const FREE_TARGETS = [
  null,
  undefined,
  { tagName: 'BODY' },
  { tagName: 'CANVAS', getAttribute: () => null },
  { tagName: 'DIV', getAttribute: () => null }
];

function describe(target) {
  const role_ = target.getAttribute?.('role') ?? null;
  const type = target.getAttribute?.('type') ?? null;
  return `${target.tagName}`
    + (target.isContentEditable ? ' (editable)' : '')
    + (role_ ? ` role=${role_}` : '')
    + (type ? ` type=${type}` : '');
}

export function runTests(mod) {
  const {
    actionForKey, isTyping, controlKind, ownsKey, stepProduct, panDistance,
    panOffset, BINDINGS, SHIFT_BINDINGS, SHORTCUTS, PAN_FRACTION, PAN_VECTORS,
    ZOOM_STEP
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const press = (key, extra = {}) => actionForKey({ key, ...extra });

  console.log('the bare keys do what the help panel says:');
  check('space plays', press(' ') === 'play');
  check('left steps back', press('ArrowLeft') === 'step-back');
  check('right steps forward', press('ArrowRight') === 'step-forward');
  check('home is the oldest frame', press('Home') === 'first');
  check('end jumps to live', press('End') === 'live');
  check('up is the previous product', press('ArrowUp') === 'product-prev');
  check('down is the next product', press('ArrowDown') === 'product-next');
  check('c toggles compare', press('c') === 'compare');
  check('[ toggles the panel', press('[') === 'panel');
  check('? opens the help', press('?') === 'help');
  check('escape closes it', press('Escape') === 'close');

  console.log('\nthe arrows are not shared with MapLibre:');
  for (const [key, want] of Object.entries(SHIFT_BINDINGS)) {
    const got = press(key, { shiftKey: true });
    check(`shift+${key} is ${want}`, got === want, `got ${got}`);
    check(`shift+${key} is not the time action`,
      got !== BINDINGS[key],
      `both resolve to ${got}, so one press would pan and step`);
  }
  check('a bare arrow is still time, not pan',
    press('ArrowUp') === 'product-prev' && press('ArrowLeft') === 'step-back');
  check('every shifted arrow has a pan vector',
    Object.keys(SHIFT_BINDINGS).every((k) => PAN_VECTORS[SHIFT_BINDINGS[k]]));

  console.log('\nzoom replaces what MapLibre did with + and -:');
  check('+ zooms in', press('+') === 'zoom-in');
  check('= zooms in, since + needs shift on a US layout',
    press('=') === 'zoom-in');
  check('- zooms out', press('-') === 'zoom-out');
  check('shift+= still zooms in, by falling through to the bare table',
    press('+', { shiftKey: true }) === 'zoom-in');
  check('the zoom step is a usable fraction of a level',
    ZOOM_STEP > 0 && ZOOM_STEP <= 1, `${ZOOM_STEP}`);

  console.log('\ntext entry takes the whole keyboard:');
  for (const target of TEXT_TARGETS) {
    const name = describe(target);
    const leaked = BOUND.filter((k) => press(k, { target }) !== null);
    check(`${name} keeps every key`, leaked.length === 0,
      leaked.length ? `${leaked.join(' ')} leaked` : '');
    check(`${name} reads as text entry`, isTyping(target) === true);
    check(`${name} is kind text`, controlKind(target) === 'text');
  }

  console.log('\nevery other control keeps only the keys it handles:');
  for (const [kind, targets] of Object.entries(KIND_TARGETS)) {
    for (const target of targets) {
      check(`${describe(target)} is kind ${kind}`,
        controlKind(target) === kind, `${controlKind(target)}`);
    }
  }
  const aButton = { tagName: 'BUTTON' };
  check('space with the play button focused does not double-fire',
    press(' ', { target: aButton }) === null);
  check('enter on a button is the button\'s',
    ownsKey(aButton, 'Enter') === true);
  check('an arrow still steps with the play button focused',
    press('ArrowRight', { target: aButton }) === 'step-forward');
  check('the zoom keys still work with a button focused',
    press('+', { target: aButton }) === 'zoom-in');
  check('the panel key still works with a button focused',
    press('[', { target: aButton }) === 'panel');

  const aSlider = { tagName: 'INPUT', getAttribute: (n) => (n === 'type' ? 'range' : null) };
  check('an arrow on the scrub slider is the slider\'s',
    press('ArrowRight', { target: aSlider }) === null);
  check('space still plays with the scrub slider focused',
    press(' ', { target: aSlider }) === 'play');
  check('a character still works with a slider focused',
    press('c', { target: aSlider }) === 'compare');

  const aSelect = { tagName: 'SELECT' };
  check('an arrow in the product picker is the picker\'s',
    press('ArrowDown', { target: aSelect }) === null);
  check('space in the product picker is the picker\'s',
    press(' ', { target: aSelect }) === null);
  check('a character in the product picker is the picker\'s, for typeahead',
    press('c', { target: aSelect }) === null);
  check('escape still closes the help with the picker focused',
    press('Escape', { target: aSelect }) === 'close');

  for (const target of FREE_TARGETS) {
    const name = target ? target.tagName : String(target);
    check(`${name} does not block the shortcuts`,
      press(' ', { target }) === 'play');
    check(`${name} is not a control`, controlKind(target) === null);
  }

  console.log('\nthe browser keeps its own shortcuts:');
  for (const mod_ of ['ctrlKey', 'metaKey', 'altKey']) {
    const leaked = BOUND.filter((k) => press(k, { [mod_]: true }) !== null);
    check(`${mod_} passes every bound key through`, leaked.length === 0,
      leaked.join(' '));
  }
  check('shift alone does not block a shortcut',
    press(' ', { shiftKey: true }) === 'play');

  console.log('\ncaps lock does not disable the letters:');
  for (const [upper, lower] of [['K', 'k'], ['J', 'j'], ['L', 'l'],
    ['C', 'c']]) {
    check(`${upper} means what ${lower} means`,
      press(upper) === press(lower) && press(upper) !== null,
      `${press(upper)} against ${press(lower)}`);
  }

  console.log('\nunbound keys are left alone:');
  for (const key of ['a', 'z', '1', 'Tab', 'F5', 'Enter', 'PageUp', 'Shift',
    ']', '/']) {
    check(`${key} is ignored`, press(key) === null, `${press(key)}`);
  }

  console.log('\nthe help panel and the bindings agree:');
  const bound = new Set([...Object.values(BINDINGS),
    ...Object.values(SHIFT_BINDINGS)]);
  const listed = SHORTCUTS.flatMap((s) => s.actions);
  const undocumented = [...bound].filter((a) => !listed.includes(a));
  const unbound = listed.filter((a) => !bound.has(a));
  check('every bound action is in the list', undocumented.length === 0,
    undocumented.length
      ? `${undocumented.join(' ')} fires with nothing telling anyone`
      : '');
  check('every listed action is bound', unbound.length === 0,
    unbound.length ? `${unbound.join(' ')} is documented and does nothing` : '');
  check('no action is listed under two rows',
    new Set(listed).size === listed.length,
    `${listed.length} entries, ${new Set(listed).size} distinct`);
  check('the list names a key for each row',
    SHORTCUTS.every((s) => s.keys.trim().length > 0));
  check('the list says what each row does',
    SHORTCUTS.every((s) => s.does.trim().length > 0));
  check('no row is listed twice',
    new Set(SHORTCUTS.map((s) => s.keys)).size === SHORTCUTS.length);

  console.log('\nproduct stepping wraps:');
  const ps = ['comp_ref', 'qpe_1hr', 'qpe_15min'];
  check('next from the middle', stepProduct(ps, 'qpe_1hr', 1) === 'qpe_15min');
  check('previous from the middle',
    stepProduct(ps, 'qpe_1hr', -1) === 'comp_ref');
  check('next from the end wraps to the first',
    stepProduct(ps, 'qpe_15min', 1) === 'comp_ref');
  check('previous from the first wraps to the end',
    stepProduct(ps, 'comp_ref', -1) === 'qpe_15min');
  check('a one-product deployment stays put',
    stepProduct(['only'], 'only', 1) === 'only');
  check('an empty list returns the current product',
    stepProduct([], 'gone', 1) === 'gone');
  check('a product the deployment dropped lands on the first',
    stepProduct(ps, 'retired', 1) === 'comp_ref');
  check('a step longer than the list still lands in it',
    ps.includes(stepProduct(ps, 'comp_ref', 7)));

  console.log('\npanning is a share of the view, not a pixel count:');
  check('one press is the pan fraction of the shorter edge',
    panDistance(1000, 800) === 800 * PAN_FRACTION);
  check('a half-width compare pane takes its width instead',
    panDistance(500, 800) === 500 * PAN_FRACTION);
  check('a press never crosses the shorter edge',
    [[1000, 800], [500, 800], [320, 1200], [2200, 1100]]
      .every(([w, h]) => panDistance(w, h) < Math.min(w, h)));
  for (const [w, h] of [[0, 0], [-10, 500], [NaN, 500], [500, NaN]]) {
    check(`a ${w} by ${h} map pans nowhere rather than by NaN`,
      panDistance(w, h) === 0);
  }

  console.log('\nthe pan offsets point the way the key does:');
  check('left shows ground to the west', panOffset('pan-left', 800, 800)[0] < 0);
  check('right shows ground to the east',
    panOffset('pan-right', 800, 800)[0] > 0);
  check('up moves the center up the screen',
    panOffset('pan-up', 800, 800)[1] < 0);
  check('down moves it down', panOffset('pan-down', 800, 800)[1] > 0);
  check('a horizontal pan does not drift vertically',
    panOffset('pan-left', 800, 600)[1] === 0);
  check('a vertical pan does not drift sideways',
    panOffset('pan-up', 800, 600)[0] === 0);
  check('opposite keys cancel',
    panOffset('pan-left', 900, 700)[0] === -panOffset('pan-right', 900, 700)[0]);
  for (const action of ['play', 'step-back', 'zoom-in', 'help']) {
    check(`${action} is not a pan`, panOffset(action, 800, 800) === null);
  }

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall key assertions passed');
  return 0;
}
