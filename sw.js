// ============================================
// 인비랩 서비스워커 (앱 설치 요건용)
// 원칙: 사이트 내용은 절대 캐시하지 않는다 (항상 최신 서버 내용 사용).
//       인터넷이 끊겼을 때만 안내 페이지(offline.html)를 보여준다.
// ============================================
const CACHE = "inbilab-pwa-v1";
const PRECACHE = ["/offline.html", "/assets/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  // 페이지 이동 요청: 항상 네트워크 우선, 실패(오프라인) 시 안내 페이지
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).catch(() => caches.match("/offline.html"))
    );
    return;
  }
  // 그 외 요청: 네트워크 그대로, 실패 시 미리 담아둔 파일이 있으면 사용
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});
