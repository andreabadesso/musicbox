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
  <label>música <input id="vol" type="range" min="0" max="100" value="45"></label>
  <span id="volval">45</span>
  <label>efeitos <input id="sfxvol" type="range" min="-30" max="12" step="1" value="-3"></label>
  <span id="sfxval">-3 dB</span>
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
    names = ordered((d.sfx || []).map(x => typeof x === 'string' ? x : x.name).filter(Boolean));
    render();
    if (!names.length) say('nenhum sfx instalado ainda');
  } catch (e) {
    say('nao consegui listar os sons: ' + e, true);
  }
}

// A ordem das teclas e explicita, e nao alfabetica.
//
// Antes eram simplesmente os dez primeiros em ordem alfabetica, e isso
// remapeava o teclado toda vez que alguem adicionava um som: instalar
// "clima" empurrou "queisso" para fora da tecla 0 sem ninguem pedir. Num
// evento a memoria muscular vale mais que a ordem, entao a lista manda.
//
// Nome que estiver aqui e nao existir no disco e ignorado, e o que sobrar
// entra depois, sem tecla. Assim da para editar esta lista sem medo.
const KEY_ORDER = [
  "airhorn", "fogo", "clima", "rapaz", "uepa",
  "laele", "zedamanga", "deuruim", "aplausos", "terminou"
];

function ordered(all) {
  const set = new Set(all);
  const first = KEY_ORDER.filter(n => set.has(n));
  const rest = all.filter(n => !first.includes(n)).sort();
  return first.concat(rest);
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

// ── Volume dos efeitos ────────────────────────────────────────────────────
// Em dB, e nao numa escala de 0 a 100, porque e isso que o mixer entende e
// traduzir aqui so criaria um numero que nao bate com o log nem com o nix.
// A faixa vai ate +12: os arquivos vem de bibliotecas de meme e alguns foram
// gravados baixo demais, entao ficar preso em 0 deixaria esses inaudiveis. O
// mixer satura de forma limpa, entao o pior caso de exagerar e distorcer, nao
// estourar em wraparound.
//
// O valor NAO fica salvo: e o ajuste da noite. Se um numero se provar bom, ele
// vira o default no nix, e ai sobrevive a reboot.
const sfxvol = document.getElementById('sfxvol');
const sfxval = document.getElementById('sfxval');

sfxvol.addEventListener('input', () => sfxval.textContent = sfxvol.value + ' dB');
sfxvol.addEventListener('change', async () => {
  try {
    const r = await fetch('/mixer/gain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({db: Number(sfxvol.value)})
    });
    const d = await r.json();
    say(d.ok ? ('efeitos em ' + d.gain_db + ' dB') : ('efeitos: ' + (d.detail || 'falhou')), !d.ok);
  } catch (e) { say('efeitos: ' + e, true); }
});

async function loadSfxGain() {
  try {
    const r = await fetch('/mixer/gain');
    const d = await r.json();
    // Number() e nao typeof: o servidor ja converte, mas confiar em tipo
    // exato vindo de JSON foi exatamente o que escreveu "sem mixer" na tela
    // com o mixer no ar.
    const db = d.ok ? Number(d.gain_db) : NaN;
    if (Number.isFinite(db)) {
      sfxvol.value = db;
      sfxval.textContent = db + ' dB';
    } else {
      // Mixer desligado: o controle nao tem o que controlar.
      sfxvol.disabled = true;
      sfxval.textContent = 'sem mixer';
    }
  } catch (e) { sfxvol.disabled = true; sfxval.textContent = 'sem mixer'; }
}

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
loadSfxGain();
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
