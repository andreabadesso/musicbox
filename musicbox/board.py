"""Soundboard: botoes e teclas de atalho para os sfx.

Existe porque apertar um som tem que ser instantaneo. Durante um evento
ninguem vai montar um curl, e pedir para um agente e otimo para "toca tal
musica" mas atravessado para "solta o airhorn AGORA": o valor esta em apertar
1 e o som sair.

Uma pagina so, sem build, sem dependencia externa, servida pelo proprio
musicbox. Nada de CDN: a caixa vive numa rede de evento que ja caiu varias
vezes, e uma pagina que precisa da internet para desenhar um botao e uma
pagina que nao funciona exatamente quando voce precisa dela.
"""

from __future__ import annotations

BOARD_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>musicbox</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px;
    background: #0e0f13; color: #e8e8ea;
    font: 16px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  }
  header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; letter-spacing: .2px; }
  #now { color: #9aa0a6; font-size: 13px; }
  #grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 10px; }
  button.pad {
    position: relative; border: 1px solid #262a33; background: #171a21; color: #e8e8ea;
    border-radius: 12px; padding: 20px 12px 16px; font-size: 15px; font-weight: 600;
    cursor: pointer; text-align: left; min-height: 84px;
    transition: transform .06s ease, background .12s ease, border-color .12s ease;
  }
  button.pad:hover { background: #1d212a; border-color: #343a46; }
  button.pad:active, button.pad.hit { transform: scale(.97); background: #2b6cb0; border-color: #4a90d9; }
  .key {
    position: absolute; top: 8px; right: 10px;
    font-size: 11px; color: #7d8590; background: #0e0f13;
    border: 1px solid #262a33; border-radius: 6px; padding: 1px 6px; font-weight: 700;
  }
  .controls { display: flex; gap: 8px; margin: 16px 0 10px; flex-wrap: wrap; align-items: center; }
  .controls button {
    background: #171a21; color: #e8e8ea; border: 1px solid #262a33;
    border-radius: 10px; padding: 9px 14px; font-size: 14px; cursor: pointer;
  }
  .controls button:hover { background: #1d212a; }
  input[type=range] { width: 160px; }
  #msg { margin-top: 12px; font-size: 13px; color: #9aa0a6; min-height: 18px; }
  #msg.err { color: #f28b82; }
  footer { margin-top: 18px; color: #5f6570; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>musicbox</h1>
  <span id="now">carregando...</span>
</header>

<div id="grid"></div>

<div class="controls">
  <button onclick="cmd('pause')">pause</button>
  <button onclick="cmd('resume')">resume</button>
  <button onclick="cmd('skip')">skip</button>
  <label>volume <input id="vol" type="range" min="0" max="100" value="45"></label>
  <span id="volval">45</span>
</div>

<div id="msg"></div>
<footer>teclas 1 a 9 e 0 disparam os dez primeiros sons. o modo e "over": a musica silencia durante o efeito e volta de onde parou.</footer>

<script>
const grid = document.getElementById('grid');
const msg = document.getElementById('msg');
let names = [];

function say(text, isErr) {
  msg.textContent = text;
  msg.className = isErr ? 'err' : '';
}

async function loadSfx() {
  try {
    const r = await fetch('/sfx');
    const d = await r.json();
    names = (d.sfx || []).map(x => typeof x === 'string' ? x : x.name).filter(Boolean);
    render();
    if (!names.length) say('nenhum sfx instalado ainda');
  } catch (e) {
    say('nao consegui listar os sons: ' + e, true);
  }
}

// As dez primeiras ganham tecla: 1 a 9 e depois 0, que e a ordem que a mao
// espera num teclado. O resto continua clicavel, so nao tem atalho.
function hotkeyFor(i) {
  if (i < 9) return String(i + 1);
  if (i === 9) return '0';
  return null;
}

function render() {
  grid.innerHTML = '';
  names.forEach((name, i) => {
    const b = document.createElement('button');
    b.className = 'pad';
    b.dataset.name = name;
    b.textContent = name;
    const k = hotkeyFor(i);
    if (k) {
      const tag = document.createElement('span');
      tag.className = 'key';
      tag.textContent = k;
      b.appendChild(tag);
    }
    b.onclick = () => fire(name, b);
    grid.appendChild(b);
  });
}

async function fire(name, el) {
  if (el) { el.classList.add('hit'); setTimeout(() => el.classList.remove('hit'), 180); }
  say('tocando ' + name + '...');
  try {
    const r = await fetch('/sfx/' + encodeURIComponent(name), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({mode: 'over'})
    });
    const d = await r.json();
    say(d.ok ? (name + ' ok') : (name + ': ' + (d.detail || d.error || 'falhou')), !d.ok);
  } catch (e) {
    say(name + ': ' + e, true);
  }
}

async function cmd(path, body) {
  try {
    const r = await fetch('/' + path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: body ? JSON.stringify(body) : undefined
    });
    const d = await r.json();
    say(d.ok ? (path + ' ok') : (path + ': ' + (d.detail || d.error || 'falhou')), !d.ok);
    refresh();
  } catch (e) { say(path + ': ' + e, true); }
}

document.addEventListener('keydown', ev => {
  // Ignora quando o foco esta num campo, senao digitar em qualquer lugar
  // dispara som. O slider de volume conta como campo.
  const tag = (ev.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const idx = ev.key === '0' ? 9 : (ev.key >= '1' && ev.key <= '9' ? Number(ev.key) - 1 : -1);
  if (idx < 0 || idx >= names.length) return;
  ev.preventDefault();
  const el = grid.children[idx];
  fire(names[idx], el);
});

const vol = document.getElementById('vol');
const volval = document.getElementById('volval');
vol.addEventListener('input', () => volval.textContent = vol.value);
vol.addEventListener('change', () => cmd('volume', {level: Number(vol.value)}));

async function refresh() {
  try {
    const r = await fetch('/now');
    const d = await r.json();
    const t = d.track || {};
    document.getElementById('now').textContent = d.ok
      ? (d.state === 'playing' && t.title ? (t.title + ' - ' + (t.artist || '')) : (d.state || '?'))
      : (d.detail || 'sem player');
    if (typeof d.volume === 'number' && document.activeElement !== vol) {
      vol.value = d.volume; volval.textContent = d.volume;
    }
  } catch (e) { document.getElementById('now').textContent = 'offline'; }
}

loadSfx();
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
