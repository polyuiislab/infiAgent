(() => {
  "use strict";
  // 左上角版本徽标：只读展示当前 infiagent 版本。4.4.0 起不再有隐藏后台入口。
  const badge = document.getElementById("infiagentVersionTrigger");
  if (!badge) return;
  fetch("/api/v1/runtime", { cache: "no-store" })
    .then((response) => (response.ok ? response.json() : null))
    .then((runtime) => {
      if (runtime && runtime.infiagent_version) badge.textContent = `v${runtime.infiagent_version}`;
    })
    .catch(() => {
      // Version display is informational and must never affect the console.
    });
})();
