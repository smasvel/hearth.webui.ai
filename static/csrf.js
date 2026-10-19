(() => {
  "use strict";

  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.content : "";
  const originalFetch = window.fetch.bind(window);

  window.fetch = (input, init = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, window.location.href);
    const method = (init.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (url.origin !== window.location.origin) {
      return originalFetch(input, init);
    }
    // Never satisfy live API reads from a stale browser cache. Bound ordinary GETs so
    // a lost network response cannot leave a page bootstrap or polling request pending
    // forever. Streaming and state-changing calls keep their caller-controlled lifetime.
    if (url.pathname.startsWith("/api/") && ["GET", "HEAD"].includes(method)) {
      init = {...init, cache: "no-store"};
      if (!init.signal && typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
        init.signal = AbortSignal.timeout(15000);
      }
    }
    if (!["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      return originalFetch(input, init);
    }
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(init.headers || {}).forEach((value, name) => headers.set(name, value));
    headers.set("X-CSRF-Token", token);
    return originalFetch(input, {...init, headers});
  };
})();
