'use strict';

const DEFAULT_BASE = 'https://chopin.weefeen.com';
const field = document.getElementById('base');
const msg = document.getElementById('msg');

function say(text) {
  msg.textContent = text;
  msg.classList.add('on');
  setTimeout(() => msg.classList.remove('on'), 1600);
}

chrome.storage.sync.get({ base: DEFAULT_BASE }, (got) => {
  field.value = got.base || DEFAULT_BASE;
});

document.getElementById('save').addEventListener('click', () => {
  let v = field.value.trim().replace(/\/+$/, '');
  if (!v) v = DEFAULT_BASE;
  /* Refused rather than stored: a base without a scheme becomes a relative
     address when the tab is opened, and the failure lands later and looks
     like the site being down. */
  if (!/^https?:\/\//i.test(v)) { say('Needs http:// or https://'); return; }
  chrome.storage.sync.set({ base: v }, () => { field.value = v; say('Saved'); });
});

document.getElementById('reset').addEventListener('click', () => {
  chrome.storage.sync.set({ base: DEFAULT_BASE }, () => {
    field.value = DEFAULT_BASE;
    say('Reset');
  });
});
