---
name: gsap
description: Use when implementing frontend animations with GSAP — timeline/tween/stagger, ScrollTrigger scrub/pin, ScrollToPlugin smooth scroll, Observer touch/wheel input, Flip shared-layout transitions, Draggable + Inertia drag, TextPlugin/ScrambleTextPlugin text effects, MotionPathPlugin path animation, @gsap/react useGSAP cleanup, v2 TweenMax/TimelineMax migration, or gsap.utils / quickTo / quickSetter utilities. Do not use for simple hover/focus states that CSS transitions can handle.
---

# GSAP 动画

## 执行原则

- 优先沿用项目已有的 GSAP 版本、导入风格和框架模式；不要为一个小动画引入新的架构。
- 简单 hover / focus / 单元素状态变化优先用 CSS `transition`；多元素编排、滚动驱动、可暂停/seek 的序列、复杂 easing 或组件卸载清理再用 GSAP。
- 新增依赖前先检查项目是否已有 `gsap`；React 项目需要自动清理时优先使用 `@gsap/react`。
- 独立 HTML demo 可以使用 CDN；不要固定旧 patch 版本。需要可复现时 pin 到项目当前版本或官方最新稳定版。
- 做滚动动画时必须注册插件，并在动态内容、图片加载或布局变化后考虑 `ScrollTrigger.refresh()`。

## 常用导入

```js
import gsap from "gsap";
import ScrollTrigger from "gsap/ScrollTrigger";

gsap.registerPlugin(ScrollTrigger);
```

React:

```jsx
import { useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";
import ScrollTrigger from "gsap/ScrollTrigger";

gsap.registerPlugin(useGSAP, ScrollTrigger);

function Comp() {
  const scope = useRef(null);

  useGSAP(() => {
    gsap.from(".item", { y: 24, opacity: 0, stagger: 0.05, duration: 0.45 });
  }, { scope });

  return <div ref={scope}>...</div>;
}
```

## v3 迁移速记

- `TweenMax` / `TweenLite` / `TimelineMax` / `TimelineLite` 已由统一的 `gsap` API 取代。
- v2: `TweenMax.fromTo(target, duration, fromVars, toVars)`
- v3: `gsap.fromTo(target, fromVars, toVars)` — duration 写在 toVars 里
- easing 使用字符串，例如 `power2.out`、`elastic.out(1, 0.3)`、`back.inOut`。

## Tween 与 Timeline

```js
gsap.to(el, { x: 100, opacity: 0, duration: 0.5, ease: "power2.out" });
gsap.from(el, { y: 30, opacity: 0, duration: 0.4 });
gsap.fromTo(el, { x: 0 }, { x: 100, duration: 0.5 });
```

多步动画用 timeline，不要用 `setTimeout` 串接，也不要用空 tween 当延迟:

```js
const tl = gsap.timeline({ defaults: { duration: 0.5, ease: "power2.out" } });

tl.to(".a", { x: 100 })
  .to(".b", { x: 100 }, "-=0.3")
  .to(".c", { x: 100 }, "+=0.2")
  .addLabel("mid")
  .to(".d", { y: 50 }, "mid");
```

重复播放、按钮触发、状态切换时，用 `fromTo` 或先 `gsap.set()` 明确初始态，避免第二次从当前残留状态开始:

```js
tl.fromTo(el, { x: -100, opacity: 0 }, { x: 0, opacity: 1, duration: 0.5 });
```

## ScrollTrigger

```js
gsap.to(".target", {
  y: -100,
  ease: "none",
  scrollTrigger: {
    trigger: ".target",
    start: "top 80%",
    end: "bottom 20%",
    scrub: 0.5,
  },
});
```

- `scrub` 把动画进度绑定到滚动位置；普通入场动画优先用 `toggleActions`。
- `pin` 会改布局，检查后续内容、移动端高度和刷新时机。
- 多个元素共享同一滚动触发条件时，优先用一个 tween/timeline + `stagger`，减少重复配置。
- 响应式滚动动画优先用 `gsap.matchMedia()`；组件卸载时必须让 context 或 hook 负责清理。

```jsx
useGSAP(() => {
  const mm = gsap.matchMedia();
  mm.add('(min-width: 768px)', () => {
    gsap.to('.el', { x: 100, scrollTrigger: { trigger: '.el', scrub: true } });
  });
  mm.add('(max-width: 767px)', () => {
    gsap.from('.el', { y: 20 });
  });
  return () => mm.revert();
}, { scope });
```

## 滚动到指定位置 (ScrollToPlugin)

锚点跳转、返回顶部、点击 tab 滚动到对应区块这类需求用 ScrollToPlugin；不要用 `window.scrollTo({ behavior: "smooth" })`，因为它不能与现有 tween 编排，不能暂停，也不能接 `autoKill`。

```js
import gsap from "gsap";
import ScrollToPlugin from "gsap/ScrollToPlugin";
gsap.registerPlugin(ScrollToPlugin);

// 滚动到绝对坐标
gsap.to(window, { duration: 1, scrollTo: 500, ease: "power2.inOut" });

// 滚动到元素，带 fixed 头部偏移
gsap.to(window, {
  duration: 0.8,
  scrollTo: { y: "#section-3", offsetY: 80, autoKill: true },
  ease: "power2.out",
});

// 容器内横向/纵向滚动
gsap.to(trackEl, { duration: 1, scrollTo: { x: 600, y: 0 } });
```

- `autoKill: true` 在用户中途手动滚动时取消动画；做长距离滚动时几乎总要加。
- `offsetY` 用来避开 fixed 头部；多 anchor 页面用同一个常量。
- 容器内滚动要传容器元素而不是 `window`。
- 列表项点击滚动时，先用 `gsap.killTweensOf(window)` 避免上一个未完成的滚动继续生效。
- 始终配合 `prefers-reduced-motion` 处理：检测到就 `target.scrollIntoView()`，不做动画。

```js
link.addEventListener("click", (e) => {
  e.preventDefault();
  const target = document.querySelector(link.getAttribute("href"));
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
    target.scrollIntoView();
  } else {
    gsap.to(window, { duration: 0.6, scrollTo: { y: target, offsetY: 80, autoKill: true } });
  }
});
```

## 触摸与滚轮事件 (Observer)

需要把 wheel / touch / pointer 输入统一成一个抽象（例如横向 carousel、视差、键盘触发）时用 Observer；**不要**自己加一堆 `wheel` / `touchstart` / `touchmove` 监听器去自己 normalize 跨设备行为。

```js
import { Observer } from "gsap/Observer";
gsap.registerPlugin(Observer);

Observer.create({
  target: window,
  type: "wheel,touch,pointer",
  preventDefault: true,
  onDown: () => gsap.to(".track", { x: "-=300", duration: 0.6 }),
  onUp:   () => gsap.to(".track", { x: "+=300", duration: 0.6 }),
});
```

- 需要的是 **scroll 位置**驱动 → 用 ScrollTrigger；需要的是 **输入事件本身** → 用 Observer。
- `type` 默认 `"wheel,touch,pointer,scroll"`；只监听子集时显式列出来能减少误触发。
- `preventDefault: true` 会拦掉默认滚动，carousel 类场景用，文档级普通滚动不要开。
- 回调里只读不写状态，状态更新走 tween；快速滚动要 debounce 时用 `self.event` 的时间戳判断。
- 组件卸载时用 `observer.kill()` 释放；多个 Observer 全局累计会显著拖慢滚动。

```js
const obs = Observer.create({ target: ".carousel", type: "wheel", onWheel: (self) => {
  if (Date.now() - (obs.lastTrigger || 0) < 600) return;
  obs.lastTrigger = Date.now();
  gsap.to(".slides", { x: `+=${self.deltaY > 0 ? 100 : -100}`, duration: 0.4 });
}});
// 卸载：obs.kill()
```

## 组件清理

React 优先用 `useGSAP`。依赖变化需要重建动画时设 `dependencies` + `revertOnUpdate: true`；事件处理器、延迟回调、手动 listener 中创建的 tween 必须用 `contextSafe()` 包裹,否则不会随 cleanup 一起 kill。

```jsx
useGSAP((scope, contextSafe) => {
  gsap.from('.item', { y: 24, opacity: 0, stagger: 0.05 });

  // 事件 handler 里的 tween 不会自动被 cleanup 跟踪
  const handleClick = contextSafe(() => {
    gsap.to(scope.current, { x: 100 });
  });
  scope.current.addEventListener('click', handleClick);
}, { scope, dependencies: [data], revertOnUpdate: true });
```

Vue / Svelte / 纯 JS 使用 `gsap.context()`:

```js
const ctx = gsap.context(() => {
  gsap.from(".item", { y: 30, opacity: 0, stagger: 0.05 });
}, containerEl);

// Vue: onUnmounted(() => ctx.revert())
// Svelte: onDestroy(() => ctx.revert())
// Plain JS: teardown 时调用 ctx.revert()
```

## 性能与可访问性

- 优先动画 `transform` 与 `opacity`。
- 避免在滚动或大列表中频繁动画 `width`、`height`、`top`、`left`、`margin` 等 layout 属性。
- `filter`、`color`、`box-shadow` 通常触发 paint；小范围可以用，大面积或高频动画要谨慎。
- 大列表使用 `stagger`、批量 selector 和较短 duration；避免逐个创建重复配置。
- 尊重 `prefers-reduced-motion`，必要时跳过动画或把 duration 设为 0。

## 常用工具方法 (`gsap.utils` / `quickTo` / `quickSetter`)

写动画时常被忽略的辅助 API，能省掉大量手写循环和 `[...nodeList]`。

```js
const items = gsap.utils.toArray(".item");           // NodeList / selector -> Array
const x    = gsap.utils.mapRange(0, 1024, -50, 50, mouseX); // 重映射坐标

const lerp  = gsap.utils.interpolate("red", "blue"); // 返回 (t) => color
const mid   = lerp(0.5);                              // "rgb(128, 0, 128)"

const wrap  = gsap.utils.wrap(0, 360);                // (val) => 0..359
const clamp = gsap.utils.clamp(0, 100);               // (val) => 0..100
const shuf  = gsap.utils.shuffle([1, 2, 3, 4]);       // 洗牌，原数组不变
const dist  = gsap.utils.distribute({ base: 0, amount: 200, from: "center", ease: "none" });
gsap.to(".dot", { x: dist, stagger: 0.05 });          // 中心放射布局

// 颜色 / 工具
gsap.utils.splitColor("#ff8040");                    // { r, g, b, hex }
gsap.utils.snap({ inc: 30 });                        // 吸附到 30 的倍数
```

`quickTo` / `quickSetter` 用于**高频更新**（鼠标跟随、滚动视差），避免每次 `gsap.to` 都新建 tween：

```js
// 跟随鼠标：会创建一次内部 tween，每次调用只更新 target
const xTo = gsap.quickTo(followerEl, "x", { duration: 0.4, ease: "power3.out" });
window.addEventListener("mousemove", (e) => xTo(e.clientX));

// 无 tween，性能最高（适合 scroll/parallax）
const setX = gsap.quickSetter(layerEl, "x", "px");
window.addEventListener("scroll", () => setX(window.scrollY * 0.5));
```

- `quickTo` 走 tween 引擎（有缓动、有 lag smoothing）；`quickSetter` 立即生效，无缓动。
- 鼠标/拖拽类 → `quickTo`；直接绑定到 scroll/resize 进度 → `quickSetter`。
- listener 内部用 `quickTo` 的，记得放进 `useGSAP` 的 `contextSafe()`，否则组件卸载不会清理。
- `distribute` 配 `from: "edges" | "center" | "random" | index` 写辐射/扇形/网格布局非常顺。

## 进阶插件 (Flip / Draggable / Text / ScrambleText / 路径 / SVG morph / 文本拆分)

下面这些是常见但非默认包含的插件，按需引入；用不到就不要注册进 bundle。

### Flip — 共享布局动画 (FLIP)

元素在 DOM 中移动、改尺寸、跨容器跳转时记录前后状态再 tween 回去。**先 capture，再改 DOM，最后 animate。**

```js
import { Flip } from "gsap/Flip";
gsap.registerPlugin(Flip);

const state = Flip.getState(".card");     // 1) 捕获当前位置/尺寸
container.append(card);                   // 2) 改 DOM（重排 / 跨容器 / 隐藏等）
Flip.from(state, {                        // 3) 从旧位置过渡到新位置
  duration: 0.6,
  ease: "power2.inOut",
  absolute: true,                         // 跨容器时用 absolute 过渡
  onComplete: () => card.style.position = "", // 完成后清掉临时 inline style
});
```

- 列表重排、卡片展开到详情、网格 reflow、共享元素过渡都用 Flip。
- 跨页面共享元素：路由切换前 `Flip.getState()` 持久化到全局，详情页挂载后 `Flip.from(state, { targets: newEl })`。
- `absolute: true` 会有 inline `position: absolute`，`onComplete` 一定要清掉，否则会影响后续布局。

### Draggable + Inertia

```js
import { Draggable } from "gsap/Draggable";
import { InertiaPlugin } from "gsap/InertiaPlugin";
gsap.registerPlugin(Draggable, InertiaPlugin);

Draggable.create(".card", {
  type: "x,y",
  bounds: ".container",
  inertia: true,           // 抬手后有惯性，需 InertiaPlugin
  onPress() { gsap.to(this.target, { scale: 1.05 }); },
  onRelease() { gsap.to(this.target, { x: 0, y: 0, duration: 0.5, ease: "back.out(1.4)" }); },
});
```

- React 组件卸载必须 kill：`Draggable.create(...)` 返回数组，记得 `dragInstance.kill()`（用 `useGSAP` 的 cleanup 或 effect return）。
- `inertia: true` 不注册 InertiaPlugin 会静默失效，控制台没有警告。
- `type: "rotation"` 用于旋转控件；`type: "x"` 仅水平。`edgeResistance` 给边界加橡皮筋效果。
- Draggable 默认会用 transform，**不要**和 ScrollTrigger pin 同一个元素上的 transform。

### TextPlugin & ScrambleTextPlugin

```js
import { TextPlugin } from "gsap/TextPlugin";
import { ScrambleTextPlugin } from "gsap/ScrambleTextPlugin";
gsap.registerPlugin(TextPlugin, ScrambleTextPlugin);

// 普通打字机 / 文字替换
gsap.to(".heading", { duration: 1, text: "Hello world", ease: "none" });

// 字符 scramble（解码/骇客风）
gsap.to(".heading", {
  duration: 1.2,
  scrambleText: {
    text: "DECODED",
    chars: "01lowerCase*",   // 干扰字符集
    speed: 0.3,
    revealDelay: 0.15,
  },
});
```

- `text` 是逐字符替换，`scrambleText` 是先乱码再定格到目标值。
- 重复触发前用 `gsap.killTweensOf(target, "text")` 或 `scrambleText` 避免叠加。
- 文本里包含 HTML 实体、emoji 或换行时先测试一次，TextPlugin 内部是按字符计算差值。
- 长文本动画（>200 字）使用 `ease: "none"` + 较短 duration，避免 scramble 抢眼。

### 路径 / SVG morph / 文本拆分（简记）

```js
import { MotionPathPlugin } from "gsap/MotionPathPlugin";
import { MorphSVGPlugin } from "gsap/MorphSVGPlugin";
import { SplitText } from "gsap/SplitText";
gsap.registerPlugin(MotionPathPlugin, MorphSVGPlugin, SplitText);

gsap.to(".plane", { duration: 4, motionPath: { path: "#route", autoRotate: true } });
gsap.to("#shapeA", { duration: 1, morphSVG: "#shapeB" });
const split = new SplitText(".heading", { type: "chars,words" });
gsap.from(split.chars, { y: 30, opacity: 0, stagger: 0.02 });
// 组件卸载：split.revert();
```

- `MorphSVGPlugin` 要求两个 path 的命令/点数尽量一致，否则浏览器会爆错。
- `SplitText` 商业插件，**必须**项目打包能解析 `gsap/SplitText` 子路径；调试阶段可临时用纯字符拆分替代。
- 路径动画里用 `autoRotate: true` 后元素 transform origin 会在元素中心，确保 SVG 元素 `transform-box: fill-box`。

## SVG 描边与高级缓动 (DrawSVG / CustomEase / GSDevTools)

这三个是真实项目里"被点名要加"频率最高的非默认插件。

### DrawSVGPlugin — 沿 SVG path 描边

用于让 SVG 线条/轮廓"画出来"（logo、签名、地图路线、流程图、图表）。它操纵的是 stroke 的可见比例，比手写 `stroke-dasharray` + `stroke-dashoffset` 短得多。

```js
import { DrawSVGPlugin } from "gsap/DrawSVGPlugin";
gsap.registerPlugin(DrawSVGPlugin);

// 从 0% 描到完整路径
gsap.from(".logo-path", { drawSVG: 0, duration: 2, ease: "power2.inOut" });

// 描一段范围（"30% 70%" 表示只显示中间 40% 的部分）
gsap.fromTo(".arc", { drawSVG: "0% 0%" }, { drawSVG: "0% 100%", duration: 1.5 });

// 配 ScrollTrigger：滚动到元素时才描
gsap.from(".map-route", {
  drawSVG: 0,
  scrollTrigger: { trigger: ".map-route", start: "top 75%", scrub: 1 },
});
```

- 元素必须有 `stroke`（或动画 `stroke-opacity`）；fill 不会被描到。
- `drawSVG` 的值是"可见段"（0-1 或 "0%" / "100%" / "30% 70%"），不是 `stroke-dasharray`。
- 已有 `stroke-dasharray` 样式时也能正常工作，不会冲突。
- SVG 元素整体加 `vector-effect: non-scaling-stroke` 可以让描边宽度不随缩放变化。
- 多次重绘要先 `gsap.set(path, { drawSVG: 0 })` 显式复位，否则会从上次残留位置开始。

### CustomEase — 视觉化自定义缓动

设计师在 Figma / AE 里调出来的曲线，power/elastic 这几个内置 ease 复刻不出来时，用 CustomEase 把 SVG path 形式的曲线注册成命名的 ease 字符串。

```js
import { CustomEase } from "gsap/CustomEase";
gsap.registerPlugin(CustomEase);

// path 格式：M0,0 C控制点... 1,1   必须以 (0,0) 开头、(1,1) 结尾
CustomEase.create("softLanding", "M0,0 C0.25,0.1 0.25,1 1,1");
gsap.from(".card", { y: 60, opacity: 0, duration: 1, ease: "softLanding" });

// 也可以包装已有 ease 得到一个带配置的副本
CustomEase.create("myElastic", "elastic.out(1, 0.3)");

// CustomWiggle / CustomBounce：程序化生成抖动/弹跳曲线
import { CustomWiggle } from "gsap/CustomWiggle";
gsap.registerPlugin(CustomWiggle);
CustomWiggle.create("wiggle", { wiggles: 6, type: "easeOut" });
```

- 曲线在 https://gsap.com/docs/v3/Eases/CustomEase 可视化编辑，复制 path 字符串即可。
- 同一名字二次 `create()` 会覆盖；想用同名但不同形状的，写在模块顶层初始化一次。
- 名字是全局的，跨组件复用没问题，但**别**起 `power1` / `elastic` 这种与内置同名的（会覆盖）。
- 多段曲线（带多个 `C` 控制点）可以表达"先快后慢再快"的多阶段过渡，**比 `keyframes` 更轻**。
- `CustomWiggle.create("wiggle", { ... })` 不带 `type` 时两端会无限抖动；通常指定 `"easeOut"` 或 `"anticipate"` 让结尾收住。

### GSDevTools — GSAP 自带的 DevTools UI

复杂 timeline 调时间点 / 给设计师 / QA 验收时用。装上后页面右下角出现一个播放器，可以 scrub、慢放、跳时间。

```js
import { GSDevTools } from "gsap/GSDevTools";
gsap.registerPlugin(GSDevTools);

const tl = gsap.timeline()
  .from(".a", { y: 30, opacity: 0 })
  .from(".b", { x: -30, opacity: 0 }, "-=0.2")
  .from(".c", { scale: 0.6, opacity: 0 }, "-=0.2");

GSDevTools.create({ animation: tl, id: "demo" });
// 不传 animation 时，GSAP 会自动收集所有命名 tween/timeline 显示在面板里
```

- 仅在**开发环境**用；`if (import.meta.env.DEV) GSDevTools.create(...)` 之类条件加载。
- `paused: true` 启动时停在第一帧，方便逐帧看。
- `timeScale: 0.5` 让面板上的播放变慢一半，调 ease 时很有用。
- 默认快捷键：`` ` ``（反引号）切换显隐；面板最右侧齿轮里有更多设置。
- UI 会注入自己的 CSS，**不要**全局 CSS reset 把它误伤；遇到面板被覆盖时给容器一个高 z-index。

## 参考示例

需要完整代码时读取这些文件，不要一次性加载无关示例:

- `references/examples/scenario1.html`: standalone ScrollTrigger hero demo。
- `references/examples/scenario2.html`: 可重复触发的 timeline 序列。
- `references/examples/scenario3.tsx`: React `useGSAP` + 异步列表入场。
- `references/examples/scenario4.js`: GSAP v2 到 v3 迁移。
- `references/examples/scenario5.html`: ScrollToPlugin 锚点导航 + reduced-motion 兜底。
- `references/examples/scenario6.html`: Observer 横向 carousel。
- `references/examples/scenario7.html`: Flip 列表重排 + 展开到详情。
- `references/examples/scenario8.html`: Draggable + Inertia 卡片拖拽。
- `references/examples/scenario9.html`: ScrambleText / TextPlugin 文字效果。
- `references/examples/scenario10.js`: `gsap.utils` + `quickTo` 鼠标跟随（纯 JS）。
- `references/examples/scenario11.html`: CustomEase 自定义曲线 + CustomWiggle。
- `references/examples/scenario12.html`: DrawSVGPlugin SVG 描边 + ScrollTrigger 联动。
- `references/examples/scenario13.html`: GSDevTools 复杂 timeline 的可视化调时间。

## 完成前检查

- 插件已注册，控制台没有 “target not found” 或插件未注册警告。
- React/Vue/Svelte 组件卸载会清理动画、ScrollTrigger、listener 和 inline style。
- 重复触发不会叠加旧 timeline，也不会从残留终态开始。
- 滚动动画在桌面和移动端的 start/end、pin、scrub 表现都正确。
- 动画不遮挡内容，不造成布局抖动，并处理了 reduced motion。

## License

GSAP 使用官方 Standard "No Charge" License，不是 MIT/Apache。GSAP 和原 Club 插件可免费用于商业项目，但仍受标准许可证约束，尤其要避免未经授权构建与 Webflow 动画构建能力竞争的可视化动画产品。许可证地址: https://gsap.com/standard-license。
