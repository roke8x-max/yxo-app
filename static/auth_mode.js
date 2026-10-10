/* 双模式前端公共判断（G10-B）。
   后端 /api/auth_meta 返回 mode："shadow"（AUTH_ENABLED=0）|"auth"（=1）。
   影子期 = 不校验身份：显示「我是」下拉、不挂 CSRF 头、不弹登录窗，等同旧版。
   翻 1 = 走登录态 UI + 401 弹窗 + CSRF 头。
   各页 init() 里统一调 applyAuthMode(mode)，不要各页各写一遍（防漂移）。
   本文件无任何业务逻辑，只做模式分发与影子身份（?user= > localStorage）存取。 */
"use strict";

var AUTH_MODE = "auth";   /* init 完成前按严格侧处理 */
var SHADOW_USERS = [];
var SHADOW_STORE_KEY = "yxo_user";

function shadowStoredUser() {
  try { return localStorage.getItem(SHADOW_STORE_KEY) || ""; } catch (e) { return ""; }
}
function storeShadowUser(u) {
  try { localStorage.setItem(SHADOW_STORE_KEY, u); } catch (e) { /* 忽略 */ }
}
/* 影子身份解析：URL ?user= 优先，其次上次选择；都没有则 ""（服务端同样保持空，不兜底） */
function resolveShadowUser() {
  try {
    var q = new URLSearchParams(location.search).get("user");
    if (q) return q;
  } catch (e) { /* 忽略 */ }
  return shadowStoredUser();
}
/* 影子期给同源 api 请求补 ?user=（服务端影子身份即取此值；无身份则不补） */
function withShadowUser(url) {
  if (AUTH_MODE !== "shadow" || typeof url !== "string") return url;
  if (/^https?:\/\//i.test(url)) return url;
  var u = resolveShadowUser();
  if (!u) return url;
  return url + (url.indexOf("?") < 0 ? "?" : "&") + "user=" + encodeURIComponent(u);
}
/* 各页 init() 统一调用：记 mode + 收用户列表；返回 mode */
function applyAuthMode(m) {
  AUTH_MODE = (m && m.mode === "shadow") ? "shadow" : "auth";
  SHADOW_USERS = (m && m.users) || [];
  return AUTH_MODE;
}
/* 填充 header 身份下拉（各页需备好 <select id="whoSel">）。
   onPick(v)：选中后的回调，不传则 location.reload()。
   返回当前解析到的影子身份（可能为 ""）。不在名单里的自报值会追加为一项（服务端同样信任）。 */
function fillWhoSel(onPick) {
  var sel = document.getElementById("whoSel");
  var cur = resolveShadowUser();
  if (!sel) return cur;
  sel.innerHTML = "";
  var ph = document.createElement("option");
  ph.value = "";
  ph.textContent = "请选择身份";
  sel.appendChild(ph);
  SHADOW_USERS.forEach(function (u) {
    var o = document.createElement("option");
    o.value = u;
    o.textContent = u;
    if (u === cur) o.selected = true;
    sel.appendChild(o);
  });
  if (cur && SHADOW_USERS.indexOf(cur) < 0) {
    var extra = document.createElement("option");
    extra.value = cur;
    extra.textContent = cur;
    extra.selected = true;
    sel.appendChild(extra);
  }
  if (!cur) sel.selectedIndex = 0;
  sel.onchange = function () {
    var v = sel.value || "";
    if (v) storeShadowUser(v);
    if (typeof onPick === "function") onPick(v);
    else location.reload();
  };
  return cur;
}
