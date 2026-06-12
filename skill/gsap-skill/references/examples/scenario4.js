// GSAP v2 -> v3 migration
// Equivalent of:
//   TweenMax.fromTo('.box', 1, {x: 0, opacity: 0}, {x: 100, opacity: 1, ease: Elastic.easeOut});
//   const tl = new TimelineMax();
//   tl.add(TweenMax.to('.a', 0.5, {y: 50}));
//   tl.add(TweenMax.to('.b', 0.5, {y: 50}));

// 1) TweenMax -> gsap namespace: v3 drops the TweenMax/TweenLite/TimelineMax classes
//    and exposes a single `gsap` object. Behaviour is identical, the API is just
//    unified.
// 2) fromTo signature changed:
//      v2: TweenMax.fromTo(target, duration, fromVars, toVars)
//      v3: gsap.fromTo(target, fromVars, toVars)  // duration moves into toVars
//    The parameter order in the new signature is (target, from, to) — not
//    (target, duration, from, to) like v2, so make sure the duration is not
//    passed as a positional second argument anymore.
// 3) Easing: `Elastic.easeOut` (a static function reference on the Ease object)
//    is gone in v3. Easings are now strings: "elastic.out". You can also pass
//    config params inline, e.g. "elastic.out(1, 0.3)" — equivalent to the
//    v2 Elastic.easeOut.config(1, 0.3).
gsap.fromTo(
  '.box',
  { x: 0, opacity: 0 }, // fromVars
  {
    x: 100,
    opacity: 1,
    duration: 1,           // duration is now part of the vars object
    ease: 'elastic.out',   // v2: Elastic.easeOut
  }
);

// 4) TimelineMax -> gsap.timeline(): v3 collapses TimelineLite/TimelineMax
//    into a single `gsap.timeline()` factory; no `new` keyword needed.
const tl = gsap.timeline();

// 5) tl.add(gsap.to(...)) still works, but the idiomatic v3 form is to chain
//    timeline methods directly — each .to() / .fromTo() returns the timeline
//    so you don't need tl.add() at all. We use the chained form below to keep
//    the file clean.
tl.to('.a', { y: 50, duration: 0.5 })
  .to('.b', { y: 50, duration: 0.5 });
