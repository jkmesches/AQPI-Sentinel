// Holding the current frame's pixels for the cursor readout.
//
// Run:  node validation_tests/js/run_map_sampler.mjs
//
// The canvas is faked here, since node has none. What the fake cannot check
// is whether a real canvas draws the image correctly, which is the browser's
// job and is covered by shoot_readout.py against the deployed console.
//
// What it does check is the part that would otherwise produce a wrong number
// quietly: a frame that fails to draw must leave no pixels behind. A stale
// frame reads back cleanly and reports a value from a different minute,
// which looks exactly like a correct reading.

function fakeCanvas(pixels) {
  // pixels: (x, y) -> [r,g,b,a]. Records what was asked of it.
  const calls = { clearRect: 0, drawImage: 0, getImageData: 0 };
  const canvas = {
    width: 0,
    height: 0,
    calls,
    failContext: false,
    failDraw: false,
    failRead: false,
    getContext(id) {
      if (id !== '2d' || canvas.failContext) return null;
      return {
        clearRect: () => { calls.clearRect++; },
        drawImage: () => {
          calls.drawImage++;
          if (canvas.failDraw) throw new Error('tainted');
        },
        getImageData: (x, y) => {
          calls.getImageData++;
          if (canvas.failRead) throw new Error('tainted canvas');
          return { data: pixels(x, y) ?? [0, 0, 0, 0] };
        }
      };
    }
  };
  return canvas;
}

const img = (w, h) => ({ naturalWidth: w, naturalHeight: h });

export function runTests(mod) {
  const { FrameSampler } = mod;
  const failures = [];
  const check = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'ok  ' : 'FAIL'}  ${label}${detail ? `  — ${detail}` : ''}`);
    if (!cond) failures.push(label);
  };
  const pix = (x, y) => [x % 256, y % 256, (x + y) % 256, 255];

  console.log('a drawn frame reads back:');
  {
    const c = fakeCanvas(pix);
    const s = new FrameSampler(() => c);
    check('nothing reads before a frame arrives', s.at(0, 0) === null);
    check('and it reports no size', s.size === null);
    check('a frame draws', s.draw(img(700, 600)) === true);
    check('the canvas took its size', c.width === 700 && c.height === 600,
      `${c.width}x${c.height}`);
    check('it reports that size',
      JSON.stringify(s.size) === '{"width":700,"height":600}');
    check('a pixel reads back',
      JSON.stringify(s.at(3, 4)) === '[3,4,7,255]',
      JSON.stringify(s.at(3, 4)));
    check('the last pixel reads back', s.at(699, 599) !== null);
  }

  console.log('\noutside the frame reads nothing:');
  {
    const s = new FrameSampler(() => fakeCanvas(pix));
    s.draw(img(700, 600));
    check('one past the right edge', s.at(700, 0) === null);
    check('one past the bottom', s.at(0, 600) === null);
    check('a negative column', s.at(-1, 0) === null);
    check('a negative row', s.at(0, -1) === null);
    check('a fractional position', s.at(3.5, 4) === null);
    check('NaN', s.at(NaN, 4) === null);
  }

  console.log('\na frame that cannot be drawn leaves nothing behind:');
  {
    // The regression this file is for. A stale frame reads back cleanly and
    // reports a value from a different minute.
    const c = fakeCanvas(pix);
    const s = new FrameSampler(() => c);
    s.draw(img(700, 600));
    check('the good frame reads', s.at(10, 10) !== null);
    c.failDraw = true;
    check('the bad frame reports failure', s.draw(img(700, 600)) === false);
    check('and nothing reads after it', s.at(10, 10) === null,
      JSON.stringify(s.at(10, 10)));
    check('and no size is reported', s.size === null);
  }

  console.log('\na missing or empty frame clears the last one:');
  {
    const s = new FrameSampler(() => fakeCanvas(pix));
    s.draw(img(700, 600));
    check('null clears it', s.draw(null) === false && s.at(10, 10) === null);
    s.draw(img(700, 600));
    check('a zero-width frame clears it',
      s.draw(img(0, 600)) === false && s.at(10, 10) === null);
    s.draw(img(700, 600));
    check('a zero-height frame clears it',
      s.draw(img(700, 0)) === false && s.at(10, 10) === null);
  }

  console.log('\na canvas that refuses to be read reports nothing:');
  {
    const c = fakeCanvas(pix);
    const s = new FrameSampler(() => c);
    s.draw(img(700, 600));
    c.failRead = true;
    check('a tainted read is caught', s.at(10, 10) === null);
    const noCtx = fakeCanvas(pix);
    noCtx.failContext = true;
    const s2 = new FrameSampler(() => noCtx);
    check('no 2d context means no frame', s2.draw(img(700, 600)) === false);
    check('and nothing reads', s2.at(0, 0) === null);
  }

  console.log('\nthe canvas is reused across frames:');
  {
    // Allocating one per frame churns memory through a playback run, which
    // on a 17-step loop at 900 ms a frame is a new 1.7 MB bitmap every
    // second.
    let made = 0;
    const c = fakeCanvas(pix);
    const s = new FrameSampler(() => { made++; return c; });
    for (let i = 0; i < 20; i++) s.draw(img(700, 600));
    check('one canvas for twenty frames', made === 1, String(made));
    check('each frame was cleared before drawing',
      c.calls.clearRect === 20, String(c.calls.clearRect));
    check('and each was drawn', c.calls.drawImage === 20,
      String(c.calls.drawImage));
  }

  console.log('\na frame of a new size resizes the canvas:');
  {
    const c = fakeCanvas(pix);
    const s = new FrameSampler(() => c);
    s.draw(img(700, 600));
    s.draw(img(512, 512));
    check('the canvas followed', c.width === 512 && c.height === 512,
      `${c.width}x${c.height}`);
    check('the old far corner is now outside', s.at(699, 599) === null);
    check('the new corner reads', s.at(511, 511) !== null);
  }

  console.log('\nclear forgets the frame:');
  {
    const s = new FrameSampler(() => fakeCanvas(pix));
    s.draw(img(700, 600));
    s.clear();
    check('nothing reads', s.at(10, 10) === null);
    check('no size', s.size === null);
    check('a later frame works again',
      s.draw(img(700, 600)) === true && s.at(10, 10) !== null);
  }

  // A 2D canvas rounds a partly transparent pixel on the way out, so the
  // sampler reads the file's own bytes when it can. Stub readers stand in
  // for WebGL; whether WebGL returns the bytes unchanged was measured in
  // three browser engines (sampler.ts).
  const bytesOf = (w, h, fill) => {
    const out = new Uint8Array(w * h * 4);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) out.set(fill(x, y), (y * w + x) * 4);
    }
    return out;
  };
  // The file's bytes for a frame: its edge pixels at alpha 128. The canvas
  // stub hands back the same pixels rounded, as a real canvas would.
  const edge = (x, y) => [x % 200, y % 200, 7, 128];
  const rounded = (x, y) => [(x % 200) + 1, y % 200, 7, 128];

  console.log('\nan opaque pixel comes from the canvas, which keeps it exact:');
  {
    let reads = 0;
    const s = new FrameSampler(() => fakeCanvas(pix), () => { reads++; return null; });
    s.draw(img(40, 30));
    check('drawing reads no bytes', reads === 0, String(reads));
    check('an opaque sample is the canvas pixel',
      s.at(12, 7)?.join() === pix(12, 7).join(), String(s.at(12, 7)));
    check('...counted exact', s.exact === true);
    check('...without reading the bytes', reads === 0, String(reads));
  }

  console.log('\na partly transparent pixel comes from the file\'s bytes:');
  {
    let reads = 0;
    const s = new FrameSampler(() => fakeCanvas(rounded), (f) => {
      reads++;
      return bytesOf(f.naturalWidth, f.naturalHeight, edge);
    });
    s.draw(img(40, 30));
    const got = s.at(12, 7);
    check('an edge sample returns the file\'s bytes, not the rounded canvas',
      got?.join() === '12,7,7,128', String(got));
    check('...and says so', s.exact === true);
    s.at(3, 4);
    check('the bytes are read once per frame', reads === 1, String(reads));
    s.draw(img(40, 30));
    s.at(3, 4);
    check('a new frame reads its own', reads === 2, String(reads));
  }

  console.log('\nwithout the bytes the canvas answers, and says it is not exact:');
  {
    const failing = new FrameSampler(() => fakeCanvas(rounded), () => null);
    failing.draw(img(40, 30));
    check('a failed read falls back to the canvas',
      failing.at(5, 6)?.join() === rounded(5, 6).join(), String(failing.at(5, 6)));
    check('...and is not exact', failing.exact === false);
    const short = new FrameSampler(() => fakeCanvas(rounded), () => new Uint8Array(12));
    short.draw(img(40, 30));
    check('bytes of the wrong size are refused',
      short.at(5, 6)?.join() === rounded(5, 6).join() && short.exact === false,
      String(short.at(5, 6)));
    const empty = new FrameSampler(() => fakeCanvas(rounded), () => bytesOf(1, 1, edge));
    check('nothing sampled is not exact', empty.exact === false);
  }

  console.log('\nno frame\'s bytes outlive it:');
  {
    let fail = false;
    const s = new FrameSampler(() => fakeCanvas(rounded),
      (f) => (fail ? null : bytesOf(f.naturalWidth, f.naturalHeight, edge)));
    s.draw(img(40, 30));
    s.at(1, 1);
    fail = true;
    s.draw(img(40, 30));
    check('the next frame does not read the last one\'s bytes',
      s.at(12, 7)?.join() === rounded(12, 7).join() && s.exact === false,
      String(s.at(12, 7)));
    fail = false;
    s.draw(img(40, 30));
    s.at(1, 1);
    s.clear();
    check('clear drops them', s.at(12, 7) === null && s.exact === false);
  }

  console.log(failures.length
    ? `\n${failures.length} FAILED: ${failures.join(', ')}`
    : '\nall sampler assertions passed');
  return failures.length ? 1 : 0;
}
