const CACHE_NAME = 'QQC-cache-v8';
const urlsToCache = [
  '/static/style_base.css',
  '/static/style_header.css',
  '/static/style_index.css',
  '/static/manifest.json',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  '/static/icons/icon_1.png',
  '/static/icons/icon_2.png',
  '/static/icons/icon_3.png',
  '/static/icons/icon_5.png',
  '/static/icons/icon_6.png',
  '/static/icons/icon_7.png',
  '/static/icons/icon_8.png',
  '/static/icons/icon_9.png',
  '/static/icons/adjustments.png',
  '/static/icons/archive.png',
  '/static/icons/clients.png',

  // Adicione outras URLs que deseja cachear
];

// Instalação do Service Worker e cache dos recursos
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => Promise.all(cacheNames.filter((name) => name !== CACHE_NAME).map((name) => caches.delete(name))))
  );
});

// Intercepta requisições e serve os recursos do cache se disponíveis
self.addEventListener('fetch', (event) => {
  event.respondWith(
    caches.match(event.request).then((response) => {
      if (response) {
        return response;
      }
      return fetch(event.request);
    })
  );
});

// Atualiza o cache quando o Service Worker é ativado
self.addEventListener('activate', (event) => {
  const cacheWhitelist = [CACHE_NAME];
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames.map((cacheName) => {
          if (!cacheWhitelist.includes(cacheName)) {
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
});
