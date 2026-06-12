// gsap.utils + quickTo / quickSetter — cursor follow + grid radiate (plain JS)
//
// Usage:
//   <script src="https://cdn.jsdelivr.net/npm/gsap@3.15/dist/gsap.min.js"></script>
//   <script src="./scenario10.js"></script>
//
// The DOM expected by this file:
//   <div id="follower"></div>
//   <div class="grid">
//     <span class="dot"></span> ... 9-25 dots in a 3..5 col grid
//   </div>
//   <div id="swatch"></div>
//   <div data-parallax="0.2"></div>  (one or more parallax layers)

// `gsap` is a global from the UMD bundle loaded above.
// Plugins used in this file (ScrollTrigger, etc.) are not needed, so no
// registerPlugin() call is required.
const gsap = window.gsap;

// --- 1) Cursor follow with quickTo --------------------------------------
// quickTo reuses one internal tween, so each mousemove just retargets it
// (instead of creating a new tween per frame, which would be a perf disaster).
const follower = document.getElementById("follower");
const xTo = gsap.quickTo(follower, "x", { duration: 0.4, ease: "power3.out" });
const yTo = gsap.quickTo(follower, "y", { duration: 0.4, ease: "power3.out" });

window.addEventListener("mousemove", (e) => {
    // remap to viewport-relative coords matching the follower's positioning
    xTo(e.clientX - follower.offsetWidth / 2);
    yTo(e.clientY - follower.offsetHeight / 2);
});

// --- 2) Dot grid that radiates from the click point ---------------------
// gsap.utils.toArray normalises NodeList -> Array.
// gsap.utils.distribute computes a per-dot x offset from a stagger formula.
const dots = gsap.utils.toArray(".dot");
const dist = gsap.utils.distribute({
    base: 0,
    amount: 80,
    from: "center",                 // or "edges" | "random" | index | number
    ease: "power2.out",
});

document.querySelector(".grid").addEventListener("click", () => {
    gsap.fromTo(
        dots,
        { scale: 0.6, opacity: 0.4 },
        {
            x: dist,
            scale: 1,
            opacity: 1,
            duration: 0.6,
            stagger: { each: 0.02, from: "center" },
            ease: "power2.out",
        }
    );
});

// --- 3) Parallax on scroll with quickSetter -----------------------------
// quickSetter is even cheaper than quickTo — no tween, no lag smoothing.
const layers = gsap.utils.toArray("[data-parallax]");
const parallaxSetters = layers.map((el) => {
    const speed = parseFloat(el.dataset.parallax); // 0.2, 0.5, etc.
    return gsap.quickSetter(el, "y", "px");
});

window.addEventListener("scroll", () => {
    layers.forEach((_el, i) => {
        const speed = parseFloat(layers[i].dataset.parallax);
        parallaxSetters[i](window.scrollY * speed);
    });
}, { passive: true });

// --- 4) Color interpolation via utils.interpolate -----------------------
// interpolate() returns a function (t) => mid-value, where t is 0..1.
// Pair it with a tween on a single numeric value to drive colour mixes.
const swatch = document.querySelector("#swatch");
if (swatch) {
    const mix = gsap.utils.interpolate("#6ee7b7", "#f472b6");
    const proxy = { t: 0 };
    gsap.to(proxy, {
        t: 1,
        duration: 4,
        repeat: -1,
        yoyo: true,
        ease: "sine.inOut",
        onUpdate: () => (swatch.style.backgroundColor = mix(proxy.t)),
    });
}

// --- 5) Reduced-motion guard --------------------------------------------
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
if (reduce.matches) {
    // Strip duration so existing tweens snap, and don't bind the parallax listener
    gsap.globalTimeline.timeScale(1000);
    // (For production, also skip binding scroll/mousemove listeners entirely.)
}
