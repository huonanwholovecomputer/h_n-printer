/* ui_timepicker_test.mjs — 时间滚轮专项测试（固定时钟 14:23，复现禁用/卡位问题）
 * 用法: node tools/ui_timepicker_test.mjs
 */
import { createRequire } from 'node:module';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const APP_DIR = 'D:/打印机项目/mobile_apps/android_app';
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png' };

const server = http.createServer(async (req, res) => {
  try {
    let p = decodeURIComponent(req.url.split('?')[0]);
    if (p === '/') p = '/index.html';
    const data = await readFile(join(APP_DIR, normalize(p).replace(/^([/\\])+/, '')));
    res.writeHead(200, { 'Content-Type': MIME[extname(p)] || 'application/octet-stream' });
    res.end(data);
  } catch {
    res.writeHead(404);
    res.end('not found');
  }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const port = server.address().port;

const browser = await chromium.launch({ executablePath: CHROME, headless: true });
const page = await browser.newPage({ viewport: { width: 430, height: 920 } });
const errors = [];
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
page.on('pageerror', (e) => errors.push(e.message));

// 固定当前时间：2026-08-12 14:23（周三，今天）
await page.addInitScript(() => {
  const FIXED = new Date('2026-08-12T14:23:00+08:00');
  const RealDate = Date;
  class MockDate extends RealDate {
    constructor(...args) { super(...(args.length ? args : [FIXED.getTime()])); }
    static now() { return FIXED.getTime(); }
  }
  window.Date = MockDate;
  localStorage.setItem('hn_token', 'mock-token');
  localStorage.setItem('hn_openid', 'mock-openid');
  localStorage.setItem('hn_role', 'admin');
  localStorage.setItem('hn_is_super', '1');
});

await page.route('https://hn-space.cn/**', (route) => {
  const path = new URL(route.request().url()).pathname;
  let body = { success: true };
  if (path === '/api/device_login') body = { success: true, token: 'mock-token', openid: 'mock-openid' };
  else if (path === '/api/me') body = { success: true, role: 'admin', is_super_admin: true, license_info: null, has_temp_access: false, temp_until: '' };
  else if (path === '/api/profile') body = { success: true, nickname: '测试管理员', avatar_url: '' };
  else if (path === '/api/pricing') body = { success: true, data: {} };
  else if (path === '/api/printer_status') body = { success: true, online: false };
  route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
});

await page.goto(`http://127.0.0.1:${port}/index.html`);
await page.waitForTimeout(1500);

const dump = await page.evaluate(() => {
  const readWheel = (id) => {
    const col = document.getElementById(id);
    const vp = col.querySelector('.wheel-viewport');
    const items = [...vp.querySelectorAll('.wheel-item')].map(el => el.textContent);
    const itemH = vp.querySelector('.wheel-item') ? vp.querySelector('.wheel-item').offsetHeight : 0;
    return { items, scrollTop: vp.scrollTop, itemH, idx: itemH ? Math.round(vp.scrollTop / itemH) : -1 };
  };
  openScheduleTimePicker();
  return {
    hour: readWheel('hourWheel'),
    minute: readWheel('minuteWheel'),
  };
});
console.log('打开选择器（固定 14:23）:');
console.log('  小时列表:', dump.hour.items.join(','));
console.log('  初始小时索引/滚动位置:', dump.hour.idx, dump.hour.scrollTop);
console.log('  分钟列表:', dump.minute.items.join(','));
console.log('  初始分钟索引/滚动位置:', dump.minute.idx, dump.minute.scrollTop);

// 场景1：滚动小时到 14（索引0）→ 分钟应从 24 开始
const s1 = await page.evaluate(() => {
  const vp = document.querySelector('#hourWheel .wheel-viewport');
  const itemH = vp.querySelector('.wheel-item').offsetHeight;
  vp.scrollTop = 0;
  vp.dispatchEvent(new Event('scroll'));
  return new Promise((resolve) => setTimeout(() => {
    const items = [...document.querySelectorAll('#minuteWheel .wheel-item')].map(el => el.textContent);
    resolve({ minuteItems: items, first: items[0], last: items[items.length - 1] });
  }, 250));
});
console.log('\n场景1: 小时=14(当前小时) → 分钟列表:', s1.minuteItems.join(','));
console.log('  首项=', s1.first, '末项=', s1.last, s1.first === '24' ? '✓ 已禁用过去分钟' : '✗ 未禁用（首项应为 24）');

// 场景2：小时=15（未来）→ 分钟全量 00-59
const s2 = await page.evaluate(() => {
  const vp = document.querySelector('#hourWheel .wheel-viewport');
  const itemH = vp.querySelector('.wheel-item').offsetHeight;
  vp.scrollTop = 1 * itemH;
  vp.dispatchEvent(new Event('scroll'));
  return new Promise((resolve) => setTimeout(() => {
    const items = [...document.querySelectorAll('#minuteWheel .wheel-item')].map(el => el.textContent);
    resolve({ first: items[0], last: items[items.length - 1], count: items.length });
  }, 250));
});
console.log('\n场景2: 小时=15(未来) → 分钟:', s2.first + '..' + s2.last, '共', s2.count, s2.first === '00' && s2.count === 60 ? '✓' : '✗');

// 场景3：快速滑动小时轮到底部，检查是否卡在 23
const s3 = await page.evaluate(() => {
  const vp = document.querySelector('#hourWheel .wheel-viewport');
  const itemH = vp.querySelector('.wheel-item').offsetHeight;
  const maxScroll = vp.scrollHeight - vp.clientHeight;
  vp.scrollTop = maxScroll;
  vp.dispatchEvent(new Event('scroll'));
  return new Promise((resolve) => setTimeout(() => {
    resolve({
      scrollTop: vp.scrollTop, maxScroll,
      idx: Math.round(vp.scrollTop / itemH),
      lastItem: [...vp.querySelectorAll('.wheel-item')].pop().textContent,
      active: vp.querySelector('.wheel-item-active') ? vp.querySelector('.wheel-item-active').textContent : null,
    });
  }, 300));
});
console.log('\n场景3: 滚到底部 → scrollTop=', s3.scrollTop, 'maxScroll=', s3.maxScroll, 'idx=', s3.idx, '末项=', s3.lastItem, '激活=', s3.active);

// 场景4：从底部向上滚一格，验证不会卡住
const s4 = await page.evaluate(() => {
  const vp = document.querySelector('#hourWheel .wheel-viewport');
  const itemH = vp.querySelector('.wheel-item').offsetHeight;
  vp.scrollTop = Math.max(0, vp.scrollTop - itemH);
  vp.dispatchEvent(new Event('scroll'));
  return new Promise((resolve) => setTimeout(() => {
    resolve({ scrollTop: vp.scrollTop, idx: Math.round(vp.scrollTop / itemH), active: vp.querySelector('.wheel-item-active') ? vp.querySelector('.wheel-item-active').textContent : null });
  }, 300));
});
console.log('场景4: 从底部上滚一格 → scrollTop=', s4.scrollTop, 'idx=', s4.idx, '激活=', s4.active);
console.log('  ', s4.idx === 8 && s4.active === '22' ? '✓ 未卡在 23' : '✗ 仍卡在 23');

// 场景5：切到未来小时滚动分钟到 30，再切回当前小时 → 分钟值 30 应保留（30 在新列表内有效）
const s5 = await page.evaluate(() => {
  const hourVp = document.querySelector('#hourWheel .wheel-viewport');
  const hItem = hourVp.querySelector('.wheel-item').offsetHeight;
  const mItem = document.querySelector('#minuteWheel .wheel-item').offsetHeight;
  const liveMin = () => {
    const vp = document.querySelector('#minuteWheel .wheel-viewport');
    const item = vp.querySelector('.wheel-item');
    const active = vp.querySelector('.wheel-item-active');
    return { scrollTop: readWheelIndex('minuteWheel'), first: item.textContent, active: active ? active.textContent : null };
  };
  // 切到 15 点
  hourVp.scrollTop = 1 * hItem;
  hourVp.dispatchEvent(new Event('scroll'));
  return new Promise((resolve) => setTimeout(() => {
    // 分钟滚到 30
    const mv = document.querySelector('#minuteWheel .wheel-viewport');
    mv.scrollTop = 30 * mItem;
    mv.dispatchEvent(new Event('scroll'));
    setTimeout(() => {
      // 切回 14 点
      hourVp.scrollTop = 0;
      hourVp.dispatchEvent(new Event('scroll'));
      setTimeout(() => {
        const items = [...document.querySelectorAll('#minuteWheel .wheel-item')].map(el => el.textContent);
        resolve({
          minuteFirst: items[0],
          minuteScrollTop: liveMin().scrollTop,
          minuteActive: liveMin().active,
          hourActive: hourVp.querySelector('.wheel-item-active') ? hourVp.querySelector('.wheel-item-active').textContent : null,
        });
      }, 250);
    }, 250);
  }, 250));
});
console.log('\n场景5: 15点选30分 → 切回14点 → 分钟首项=', s5.minuteFirst, '选中=', s5.minuteActive, '滚动索引=', s5.minuteScrollTop, '小时激活=', s5.hourActive);
console.log('  ', s5.minuteActive === '30' && s5.minuteFirst === '24' ? '✓ 分钟值保留' : '✗ 分钟被重置');

// 场景6：明天 → 分钟不裁剪，换小时分钟选择保留
const s6 = await page.evaluate(() => {
  printState.scheduleDayIndex = 1;
  openScheduleTimePicker();
  const hourVp = document.querySelector('#hourWheel .wheel-viewport');
  const hItem = hourVp.querySelector('.wheel-item').offsetHeight;
  const mItem = document.querySelector('#minuteWheel .wheel-item').offsetHeight;
  const liveMin = () => {
    const vp = document.querySelector('#minuteWheel .wheel-viewport');
    const item = vp.querySelector('.wheel-item');
    return { idx: readWheelIndex('minuteWheel'), first: item.textContent, count: vp.querySelectorAll('.wheel-item').length };
  };
  const init = liveMin();
  const mv = document.querySelector('#minuteWheel .wheel-viewport');
  mv.scrollTop = 30 * mItem;
  mv.dispatchEvent(new Event('scroll'));
  return new Promise((resolve) => setTimeout(() => {
    hourVp.scrollTop = 5 * hItem;
    hourVp.dispatchEvent(new Event('scroll'));
    setTimeout(() => {
      const after = liveMin();
      resolve({ init, after, hourActive: hourVp.querySelector('.wheel-item-active') ? hourVp.querySelector('.wheel-item-active').textContent : null });
    }, 350);
  }, 250));
});
console.log('\n场景6: 明天分钟滚到30 → 换小时到05 → 分钟索引=', s6.after.idx, '首项=', s6.after.first, '数量=', s6.after.count);
console.log('  ', s6.after.first === '00' && s6.after.count === 60 && s6.after.idx === 30 ? '✓ 分钟保留且不裁剪' : '✗ 行为不符');

// 场景7：15点选10分 → 切回14点（当前小时）→ 10 已过 → 回退到首个可用分钟 24
const s7 = await page.evaluate(() => {
  printState.scheduleDayIndex = 0;
  openScheduleTimePicker();
  const hourVp = document.querySelector('#hourWheel .wheel-viewport');
  const hItem = hourVp.querySelector('.wheel-item').offsetHeight;
  const mItem = document.querySelector('#minuteWheel .wheel-item').offsetHeight;
  const liveActive = () => {
    const vp = document.querySelector('#minuteWheel .wheel-viewport');
    const a = vp.querySelector('.wheel-item-active');
    return { active: a ? a.textContent : null, first: vp.querySelector('.wheel-item').textContent };
  };
  hourVp.scrollTop = 1 * hItem;
  hourVp.dispatchEvent(new Event('scroll'));
  return new Promise((resolve) => setTimeout(() => {
    const mv = document.querySelector('#minuteWheel .wheel-viewport');
    mv.scrollTop = 10 * mItem;
    mv.dispatchEvent(new Event('scroll'));
    setTimeout(() => {
      hourVp.scrollTop = 0;
      hourVp.dispatchEvent(new Event('scroll'));
      setTimeout(() => resolve(liveActive()), 350);
    }, 250);
  }, 250));
});
console.log('\n场景7: 15点选10分 → 切回14点 → 选中=', s7.active, '首项=', s7.first);
console.log('  ', s7.active === '24' ? '✓ 无效分钟已回退到首个可用' : '✗ 未回退');

// 场景8：保存过 15:30 → 重新打开 → 分钟应还原为 30
const s8 = await page.evaluate(() => {
  printState.scheduleDayIndex = 0;
  printState.scheduleTime = '15:30';
  openScheduleTimePicker();
  const hourActive = document.querySelector('#hourWheel .wheel-item-active') ? document.querySelector('#hourWheel .wheel-item-active').textContent : null;
  const minuteActive = document.querySelector('#minuteWheel .wheel-item-active') ? document.querySelector('#minuteWheel .wheel-item-active').textContent : null;
  return { hourActive, minuteActive };
});
console.log('\n场景8: 保存过 15:30 → 重新打开 → 小时=', s8.hourActive, '分钟=', s8.minuteActive);
console.log('  ', s8.hourActive === '15' && s8.minuteActive === '30' ? '✓ 保存时间完整还原' : '✗ 还原不完整');

// 场景9：全量分钟列表高索引位（55-58）不应错位（回归：offsetHeight 四舍五入导致 56→57 高亮）
const s9 = await page.evaluate(async () => {
  printState.scheduleDayIndex = 1; // 明天 → 分钟 00-59 全量
  openScheduleTimePicker();
  await new Promise((r) => setTimeout(r, 300));
  const out = [];
  for (const k of [54, 55, 56, 57, 58]) {
    const vp = document.querySelector('#minuteWheel .wheel-viewport');
    const el = vp.querySelectorAll('.wheel-item')[k];
    const h = el.offsetHeight;
    vp.scrollTop = Math.max(0, el.offsetTop + h / 2 - vp.clientHeight / 2);
    vp.dispatchEvent(new Event('scroll'));
    await new Promise((r) => setTimeout(r, 600));
    const l = vp.querySelectorAll('.wheel-item');
    const active = vp.querySelector('.wheel-item-active');
    out.push({
      target: k,
      itemText: l[k] ? l[k].textContent : null,
      active: active ? active.textContent : null,
      computedIdx: readWheelIndex('minuteWheel'),
      scrollTop: Math.round(vp.scrollTop),
    });
  }
  return out;
});
console.log('\n场景9: 全量分钟高索引位高亮:');
let s9ok = true;
for (const r of s9) {
  const ok = r.active === r.itemText && r.computedIdx === r.target;
  if (!ok) s9ok = false;
  console.log(`  target=${r.target} → 高亮=${r.active} 计算索引=${r.computedIdx} ${ok ? '✓' : '✗'}`);
}
console.log('  ', s9ok ? '✓ 高索引位无错位' : '✗ 存在错位');

console.log('\nJS 错误:', errors.length ? errors.join('\n') : '无');
await browser.close();
server.close();
