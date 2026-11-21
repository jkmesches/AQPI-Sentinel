// What the console does when there is not enough room for all of it.
//
// Run:  node validation_tests/js/run_map_responsive.mjs
//
// Three parts of the layout have to agree about when space has run out.
// The panel, the comparison panes and the time strip each decide for
// themselves, and if their thresholds crossed there would be a band of
// widths where the panes have stacked and the panel still thinks it has
// room for 280 px beside a map.
//
// The first-visit rule is the other thing worth pinning. A reader who
// opened the panel on a narrow screen meant to open it, so a layout that
// closed it again on every load would make the control look broken.

export function runTests(mod) {
  const {
    shouldClosePanel, shouldStackPanes, shouldWrapStrip,
    PANEL_AUTOCLOSE_PX, PANE_STACK_PX, STRIP_WRAP_PX
  } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };

  console.log('the thresholds are ordered so no width is contradictory:');
  {
    check('a pane too narrow for the panel is also too narrow to compare',
      PANEL_AUTOCLOSE_PX < PANE_STACK_PX,
      `${PANEL_AUTOCLOSE_PX} and ${PANE_STACK_PX}`);
    check('the strip wraps before the panes stack would leave it cramped',
      STRIP_WRAP_PX < PANE_STACK_PX,
      `${STRIP_WRAP_PX} and ${PANE_STACK_PX}`);
    check('the panel closes before the strip wraps',
      PANEL_AUTOCLOSE_PX < STRIP_WRAP_PX,
      `${PANEL_AUTOCLOSE_PX} and ${STRIP_WRAP_PX}`);
    check('the panel threshold is twice the panel itself',
      PANEL_AUTOCLOSE_PX === 560, String(PANEL_AUTOCLOSE_PX));
  }

  console.log('\nthe panel starts closed only where it would cover the map:');
  {
    check('a phone closes it', shouldClosePanel(390, false) === true);
    check('a small tablet closes it', shouldClosePanel(540, false) === true);
    check('a laptop leaves it open',
      shouldClosePanel(1400, false) === false);
    check('exactly at the threshold it stays open',
      shouldClosePanel(PANEL_AUTOCLOSE_PX, false) === false);
    check('one pixel under it closes',
      shouldClosePanel(PANEL_AUTOCLOSE_PX - 1, false) === true);
  }

  console.log('\na reader who has been here before keeps their choice:');
  {
    check('stored prefs win on a phone',
      shouldClosePanel(390, true) === false);
    check('...and on a laptop', shouldClosePanel(1400, true) === false);
  }

  console.log('\nthe panes stack only where side by side is useless:');
  {
    check('a phone stacks', shouldStackPanes(390) === true);
    check('a tablet stacks', shouldStackPanes(820) === true);
    check('a laptop does not', shouldStackPanes(1400) === false);
    check('exactly at the threshold it does not',
      shouldStackPanes(PANE_STACK_PX) === false);
    check('one pixel under it does',
      shouldStackPanes(PANE_STACK_PX - 1) === true);
  }

  console.log('\nthe strip wraps where its axis would be hairlines:');
  {
    check('a phone wraps', shouldWrapStrip(390) === true);
    check('a laptop does not', shouldWrapStrip(1400) === false);
    check('exactly at the threshold it does not',
      shouldWrapStrip(STRIP_WRAP_PX) === false);
  }

  console.log('\na width that is not a width changes nothing:');
  {
    for (const bad of [0, -100, NaN, Infinity, -Infinity]) {
      check(`${bad} leaves the panel open`,
        shouldClosePanel(bad, false) === false);
      check(`${bad} leaves the panes side by side`,
        shouldStackPanes(bad) === false);
      check(`${bad} leaves the strip on one row`,
        shouldWrapStrip(bad) === false);
    }
  }

  if (failures.length) {
    console.log(`\n${failures.length} failed:`);
    for (const f of failures) console.log(`  ${f}`);
    return 1;
  }
  console.log('\nall responsive assertions passed');
  return 0;
}
