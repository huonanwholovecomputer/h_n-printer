/* eslint-disable */
/**
 * 小程序「安卓物理返回键」守卫的状态机仿真（utils/back_guard.js 的行为验收）
 *
 * 配套 `other/tests/verify_mp_back_guard.py`（那只管**静态约定**：四处清单一致、方法存在、
 * 递归闸在代码里）。本脚本管**行为**：把微信对 `<page-container>` 的契约模型化，跑真实
 * `utils/back_guard.js`，证明下面几件事真的成立 —— 这些正是改动时最容易搞坏、而且
 * 在真机上一眼看不出来的地方。
 *
 * 被模型化的微信契约（依据官方文档 + 2022-02-21 varzy.me 的实测）：
 *   ① 页面里有 show=true 的 page-container 时，用户按**物理返回键/右滑** → 微信**关闭容器、
 *      不关页面**，并触发 `bind:beforeleave`；此时容器的 show 表达式**可能仍为 true**
 *      （比如还有下层弹层开着）—— 但微信**不会**自己把它重新打开。
 *   ② 代码把 show 从 true 改成 false（页面自己收了最后一层弹层）同样触发 beforeleave。
 *   ③ 容器只在 show 表达式发生 **false→true** 跳变时重新挂载。
 *
 * 判据：
 *   A 一个弹层：按 1 次返回 = 只收 1 层；再按 1 次 = 不再拦截（交回页面栈/退出小程序）
 *   B 两个弹层：按第 1 次只收最上面那层，且**守卫被重新挂上**（否则第 2 层会漏过去直接退 App）；
 *     第 2 次收下面那层；第 3 次才不拦截
 *   C 递归闸：收层过程中 setData 引发的 beforeleave 不得再收一层（否则一次返回连关两层）
 *   D 嵌套标志：`pv.visible`（预览层）/ `cropVisible` 这类一样能收到
 *   E closing 吞掉：某层正在播退场动画（closing 标志为 true）时，连按不重复调用收尾
 *   F 页面自己收完最后一层 → 容器自动关闭，不得触发任何收层调用、不得死循环
 *   G 最底层（无弹层）→ leave() 返回 false，不改任何状态
 *
 * 用法：node other/tests/verify_mp_back_guard.js        （exit 0 = 全绿）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');
const GUARD_JS = path.join(REPO, 'mobile_apps', 'h_n_print', 'utils', 'back_guard.js');

const fails = [];
function check(ok, msg) {
  if (!ok) fails.push(msg);
  console.log('[' + (ok ? 'PASS' : 'FAIL') + '] ' + msg);
}

/* ── 载入真实 utils/back_guard.js（跑的是仓库里的实现，不是复制品） ── */
function loadGuard() {
  const src = fs.readFileSync(GUARD_JS, 'utf8');
  const sandbox = { module: { exports: {} }, console: console, exports: {} };
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: GUARD_JS });
  return sandbox.module.exports;
}
const guard = loadGuard();

/* ── 微信 page-container 契约的模型 ── */
function setPath(data, key, value) {
  // 与 setData 一致：支持 'pv.visible' 这种一层嵌套路径
  if (key.indexOf('.') < 0) { data[key] = value; return; }
  const parts = key.split('.');
  let cur = data;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null || typeof cur[parts[i]] !== 'object') cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

function resolveFlag(data, flag) {
  if (flag.indexOf('.') < 0) return data[flag];
  const parts = flag.split('.');
  let cur = data;
  for (let i = 0; i < parts.length; i++) {
    if (cur == null) return undefined;
    cur = cur[parts[i]];
  }
  return cur;
}

class Sim {
  constructor(opts) {
    this.layers = opts.layers;
    this.page = {
      data: Object.assign({ backGuardHold: false }, opts.data || {}),
      calls: [],
      // 收层方法：按名字调用（与真实页面 methods 里的写法一致）
      _simClose: null,
    };
    const self = this;
    this.page.setData = function (patch, cb) {
      Object.keys(patch).forEach((k) => { setPath(self.page.data, k, patch[k]); });
      // 先让视图反应（挂载/关闭容器；关闭容器会触发 beforeleave），再回调 ——
      // 顺序反了就不是微信的语义了：remount 的 true→false 假推会被回调提前抹平。
      self.sync();
      if (cb) cb();
    };
    this.layers.forEach((item) => {
      if (typeof item.close === 'function') return;
      const name = item.close;
      self.page[name] = function () { self.page.calls.push(name); self._closeImpl(name); };
    });
    // 页面实例的 beforeleave 处理器：与真实页面一样只做一次转调
    this.page.onBackGuardLeave = function () {
      return guard.leave(self.page, self.layers);
    };
    this.mounted = false;
    this.exprPrev = false;
    this.leaveDepth = 0;
    this.leaveFires = 0;
    this.sync();
  }

  expr() {
    const self = this;
    if (this.page.data.backGuardHold) return false;
    for (let i = 0; i < this.layers.length; i++) {
      if (resolveFlag(this.page.data, this.layers[i].flag)) return true;
    }
    return false;
  }

  // 表达式变化 → 微信挂载 / 关闭容器（关闭时会触发 beforeleave）
  sync() {
    const e = this.expr();
    if (e === this.exprPrev) return;
    if (e) {
      this.mounted = true;
    } else {
      this.mounted = false;
      this.exprPrev = e;
      this.fireLeave();
      return;
    }
    this.exprPrev = e;
  }

  fireLeave() {
    this.leaveFires += 1;
    if (this.leaveDepth > 20) throw new Error('beforeleave 递归失控（守卫递归闸失效）');
    this.leaveDepth += 1;
    try {
      this.page.onBackGuardLeave();
    } finally {
      this.leaveDepth -= 1;
    }
  }

  // 模拟该层收尾方法（默认：直接置 flag=false，跟真实页面同步关的那类一致）
  _closeImpl(name) {
    const item = this.layers.filter((l) => l.close === name)[0];
    if (!item) return;
    const self = this;
    if (item.asyncClose) {
      // 200ms 退场动画：先置 closing，再置 flag=false
      if (item.closing) self.page.setData({ [item.closing]: true });
      self.page.__pending = self.page.__pending || [];
      self.page.__pending.push(() => {
        const patch = {};
        patch[item.flag] = false;
        if (item.closing) patch[item.closing] = false;
        self.page.setData(patch);
      });
    } else {
      const patch = {};
      patch[item.flag] = false;
      self.page.setData(patch);
    }
  }

  flush() {
    const pend = this.page.__pending || [];
    this.page.__pending = [];
    pend.forEach((fn) => fn());
  }

  /** 用户按一次安卓物理返回键：容器在 → 微信关容器 + beforeleave；不在 → 交回页面栈 */
  pressBack() {
    if (!this.mounted) return 'page-or-exit';
    this.mounted = false;
    this.fireLeave();
    return 'intercepted';
  }
}

/* ── A：单个弹层 ── */
{
  const layers = [{ flag: 'showConfirmModal', close: 'onCancelConfirm', closing: 'modalClosing' }];
  const sim = new Sim({ layers, data: { showConfirmModal: true, modalClosing: false } });
  check(sim.mounted === true, 'A1 弹层打开时守卫挂上（容器 show=true）');
  const r1 = sim.pressBack();
  sim.flush();
  check(r1 === 'intercepted', 'A2 第一次返回被容器拦下（不退出小程序）');
  check(sim.page.data.showConfirmModal === false, 'A3 弹层被收掉');
  check(sim.page.calls.filter((c) => c === 'onCancelConfirm').length === 1,
    'A4 收层方法只调用一次（递归闸生效）');
  check(sim.mounted === false, 'A5 弹层收完后守卫不再挂着（下一次返回才能交回页面栈）');
  check(sim.pressBack() === 'page-or-exit', 'A6 最底层再按返回 → 交给页面栈/退出小程序（与 App 同口径）');
}

/* ── B：两层弹层 —— 一次只收一层，且守卫要重新挂上 ── */
{
  const layers = [
    { flag: 'pv.visible', close: 'onPvClose' },
    { flag: 'showConfirmModal', close: 'onCancelConfirm', closing: 'modalClosing' },
  ];
  const sim = new Sim({ layers, data: { pv: { visible: true }, showConfirmModal: true, modalClosing: false } });
  sim.pressBack();
  sim.flush();
  check(sim.page.data.pv.visible === false, 'B1 第 1 次返回收掉最上层（全屏预览层）');
  check(sim.page.data.showConfirmModal === true, 'B2 下层弹层**没有被一起收掉**（递归闸关键作用）');
  check(sim.mounted === true, 'B3 收完一层后守卫被重新挂上（不重挂第 2 层就漏过去直接退小程序）');
  sim.pressBack();
  sim.flush();
  check(sim.page.data.showConfirmModal === false, 'B4 第 2 次返回收掉更下层');
  check(sim.mounted === false, 'B5 弹层全收完，守卫撤下');
  check(sim.pressBack() === 'page-or-exit', 'B6 第 3 次返回才交回页面栈');
  check(sim.page.calls.join(',') === 'onPvClose,onCancelConfirm', 'B7 收层顺序 = 栈序（从上到下）');
}

/* ── C：递归闸 —— 代码关容器也会触发 beforeleave，不得顺手再收一层 ── */
{
  const layers = [
    { flag: 'showMyFiles', close: 'onCloseMyFiles', closing: 'myFilesClosing', asyncClose: true },
    { flag: 'showPricePop', close: 'onClosePricePop' },
  ];
  const sim = new Sim({ layers, data: { showMyFiles: true, myFilesClosing: false, showPricePop: true } });
  sim.pressBack();
  check(sim.page.calls.length === 1, 'C1 一次返回只调一次收层（hold 闸挡住 remount 引发的第二次 beforeleave）');
  sim.flush();
  check(sim.page.data.showMyFiles === false && sim.page.data.showPricePop === true,
    'C2 退场动画结束后只关了最上层，下层完好');
  check(sim.mounted === true, 'C3 剩余弹层仍被守卫罩住');
}

/* ── D：嵌套标志与无 closing 的层 ── */
{
  const layers = [{ flag: 'cropVisible', close: 'onCropCancel' }];
  const sim = new Sim({ layers, data: { cropVisible: true } });
  sim.pressBack();
  check(sim.page.data.cropVisible === false && sim.page.calls.length === 1,
    'D1 cropVisible（头像裁剪层）能正常收到（无 closing、同步关）');
}

/* ── E：closing 吞掉重复返回 ── */
{
  const layers = [{ flag: 'fileDetailVisible', close: 'onCloseFileDetail', closing: 'fileDetailClosing', asyncClose: true }];
  const sim = new Sim({ layers, data: { fileDetailVisible: true, fileDetailClosing: false } });
  sim.pressBack();                       // 第一次：进入退场动画（closing=true，flag 还没落）
  check(sim.page.data.fileDetailClosing === true, 'E1 收层进入退场动画（closing=true）');
  check(sim.mounted === true, 'E2 动画期间守卫仍挂着');
  const before = sim.page.calls.length;
  sim.pressBack();                       // 动画里连按：应被 closing 吞掉
  check(sim.page.calls.length === before, 'E3 连按返回不会重复触发收尾（closing 吞掉）');
  sim.flush();
  check(sim.page.data.fileDetailVisible === false, 'E4 动画结束后弹层收掉');
}

/* ── F：页面自己收完最后一层 —— 容器自动关闭，不得触发收层、不得死循环 ── */
{
  const layers = [{ flag: 'showPricePop', close: 'onClosePricePop' }];
  const sim = new Sim({ layers, data: { showPricePop: true } });
  sim.page.setData({ showPricePop: false });   // 用户点了气泡里的「关闭」
  check(sim.page.calls.length === 0, 'F1 页面自己收掉最后一层时，守卫不得再调一次收层方法');
  check(sim.mounted === false, 'F2 容器随表达式关闭');
  check(sim.pressBack() === 'page-or-exit', 'F3 之后按返回直接交回页面栈（不死循环）');
}

/* ── G：最底层无弹层 ── */
{
  const layers = [{ flag: 'showStatusPop', close: 'onCloseStatusPop' }];
  const sim = new Sim({ layers, data: { showStatusPop: false } });
  check(sim.mounted === false, 'G1 无弹层时守卫不显示（不挡点击、不吞返回）');
  const ret = guard.leave(sim.page, layers);
  check(ret === false, 'G2 leave() 在没有可收的层时返回 false');
  check(sim.page.data.backGuardHold === false, 'G3 不留下 hold 残状态');
}

if (fails.length) {
  console.log('\n── 失败 ' + fails.length + ' 项 ──');
  fails.forEach((f) => console.log('  [FAIL] ' + f));
  process.exit(1);
}
console.log('\n全部通过');
