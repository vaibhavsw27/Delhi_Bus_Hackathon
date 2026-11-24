/*
  web/sw.js - service worker that makes BusPulse installable and opens fast.
  Inputs: requests from the page. Outputs: cached app shell; API calls always go to the
  network so journey times are never stale.
*/
"use strict";

const CACHE_NAME = "buspulse-shell-v5";
const SHELL = ["/", "/static/style.css", "/static/app.js", "/static/icon.svg", "/static/icon-192.png",
  "/static/icon-512.png", "/manifest.webmanifest"];

self.addEventListener("install", function onInstall(event) {
  event.waitUntil(caches.open(CACHE_NAME).then(function addShell(cache) {
    return cache.addAll(SHELL);
  }));
  self.skipWaiting();
});

self.addEventListener("activate", function onActivate(event) {
  event.waitUntil(caches.keys().then(function dropOld(keys) {
    const removals = [];
    for (let i = 0; i < keys.length; i++) {
      if (keys[i] !== CACHE_NAME) {
        removals.push(caches.delete(keys[i]));
      }
    }
    return Promise.all(removals);
  }));
  self.clients.claim();
});

self.addEventListener("fetch", function onFetch(event) {
  const url = new URL(event.request.url);
  // Only the app shell is served from cache, network first so updates arrive quickly.
  if (url.origin !== self.location.origin || url.pathname.indexOf("/api/") === 0) {
    return;
  }
  event.respondWith(fetch(event.request).then(function keepCopy(response) {
    const copy = response.clone();
    caches.open(CACHE_NAME).then(function store(cache) {
      cache.put(event.request, copy);
    });
    return response;
  }).catch(function fromCache() {
    return caches.match(event.request);
  }));
});
