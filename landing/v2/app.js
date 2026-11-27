function applyDataStyles(root){root.querySelectorAll("[data-w]").forEach(function(e){e.style.width=e.dataset.w+"%";if(e.dataset.bg)e.style.background=e.dataset.bg;});}

const MODES = {
  loop: {
    ledDone: ["cut","BLOCKED · CALL #4"],
    hudK: "4× Loop Breaker", tag: "Live test · 13 Nov",
    btn: "/agentcap:approve (+50%)",
    steps: [
      {t:'<span class="c">$ claude -p "fix flaky auth tests"</span>', v:"0 of 4", sub:"watching", boxes:[0,0,0,0], kv:[["rule","4× identical"],["last step","—"],["prompts","Never stored"]]},
      {t:'<span class="d">● Bash</span> pytest tests/auth.py  <span class="c">→ exit 1</span>', v:"1st call", sub:"attempt 1", boxes:[1,0,0,0], kv:[["rule","4× identical"],["last step","Bash: pytest"],["status","running"]]},
      {t:'<span class="d">● Bash</span> pytest tests/auth.py  <span class="c">→ exit 1</span>  <span class="d">(2×)</span>', v:"2nd call", sub:"repeating", boxes:[1,1,0,0], kv:[["rule","4× identical"],["last step","Bash: pytest"],["status","watching"]]},
      {t:'<span class="d">● Bash</span> pytest tests/auth.py  <span class="c">→ exit 1</span>  <span class="d">(3×)</span>', v:"3rd call", sub:"1 repeat left", boxes:[1,1,1,0], kv:[["rule","4× identical"],["last step","Bash: pytest"],["status","warning"]]},
      {cut:'<span><b>■ STOPPED ON LOOP</b> · 4th identical Bash call blocked</span><span class="js-ok">exit 4</span>', v:'<span class="acc">4th call</span>', sub:"blocked before run", boxes:[1,1,1,2], trip:true, kv:[["why","Agent was looping","r"],["live test","4 of 4 loops stopped","g"],["prompts","Never stored","g"]]}
    ]
  },
  quota: {
    ledDone: ["cut","STOPPED · 5.0%"],
    hudK: "5-Hour Quota Cap", tag: "Claude Pro",
    btn: "/agentcap:approve (+2.5%)",
    steps: [
      {t:'<span class="c">&gt; /agentcap:quota 5% 5h</span>', v:"0.0%", sub:"cap: 5.0%", pct:0, kv:[["window","5-hour"],["stops at","27% (22% + 5%)"],["status","armed"]]},
      {t:'<span class="g">agentcap: 5-hour quota at 22% now → stops at 27%</span>', v:"0.0%", sub:"cap: 5.0%", pct:5, kv:[["window","5-hour"],["stops at","27% (22% + 5%)"],["status","watching"]]},
      {t:'<span class="d">● Edit</span> refactoring 14 modules... <span class="d">(+2.6%)</span>', v:"2.6%", sub:"cap: 5.0%", pct:38, kv:[["window","5-hour"],["used","2.6% / 5.0%"],["status","running"]]},
      {t:'<span class="c">▲ 80% of job quota used (4.1% / 5.0%)</span>', v:"4.1%", sub:"cap: 5.0%", pct:58, kv:[["window","5-hour"],["used","4.1% / 5.0%"],["status","warning"]]},
      {cut:'<span><b>■ LIMIT REACHED</b> · 5.0% / 5.0% · next tool denied</span><span class="js-ok">receipt saved</span>', v:'<span class="acc">5.0%</span>', sub:"73% quota left", pct:72, trip:true, kv:[["why","Reached your limit","r"],["session","Held open","g"],["prompts","Never stored","g"]]}
    ]
  },
  waste: {
    ledDone: ["","READ-ONLY · FREE"],
    hudK: "7-Day Waste Audit", tag: "Local logs",
    btn: "Copy: pipx install agentcap-cli",
    steps: [
      {t:'<span class="c">$ agentcap waste --since 7d</span>', v:"...", sub:"scanning local logs", bars:[], kv:[["source","~/.claude + ~/.codex"],["uploaded","0 bytes","g"]]},
      {t:'<span class="d">SUBAGENTS</span>        <span class="c">$41.20</span>  <span class="d">25% in 31 forked threads</span>', v:"$41.20", sub:"subagents", bars:[["Subagents","$41.20",84,"#17170f"]], kv:[["source","local JSONL"],["uploaded","0 bytes","g"]]},
      {t:'<span class="d">REPEATED CALLS</span>    <span class="r">$9.80</span>  <span class="d">12 streaks ≥4 identical</span>', v:"$51.00", sub:"+ loops", bars:[["Subagents","$41.20",84,"#17170f"],["Loops","$9.80",34,"#b23a1d"]], kv:[["source","local JSONL"],["uploaded","0 bytes","g"]]},
      {t:'<span class="d">CACHE TTL MISSES</span>  <span class="c">$6.10</span>  <span class="d">57 cold re-writes (5m TTL)</span>', v:'<span class="acc">$61.40</span>', sub:"avoidable burn", bars:[["Subagents","$41.20",84,"#17170f"],["Loops","$9.80",34,"#b23a1d"],["Cache TTL","$6.10",22,"#6b6855"],["Errors","$4.30",16,"#b23a1d"]], kv:[["mode","Read-only (Free)","g"],["uploaded","0 bytes","g"]], trip:true},
      {t:'<span class="g">✓ Pro would have stopped the 11× Bash loop at call #4.</span>', v:'<span class="acc">$61.40</span>', sub:"38% of 7d spend", bars:[["Subagents","$41.20",84,"#17170f"],["Loops","$9.80",34,"#b23a1d"],["Cache TTL","$6.10",22,"#6b6855"],["Errors","$4.30",16,"#b23a1d"]], kv:[["mode","Read-only (Free)","g"],["uploaded","0 bytes","g"]], trip:true}
    ]
  }
};

let curMode = "quota", timers = [];
const streamEl = document.getElementById("stage-stream");
const ledEl = document.getElementById("stage-led");
const ledTxt = document.getElementById("stage-led-txt");
const hudBtn = document.getElementById("hud-act");

function renderViz(mode, step){
  const viz = document.getElementById("hud-viz");
  if(mode === "loop"){
    viz.innerHTML = `<div class="steps-4">${step.boxes.map((b,i)=>`<div class="sb ${b===1?'hit':b===2?'cut':''}">${i+1}</div>`).join("")}</div>`; applyDataStyles(viz);
  } else if(mode === "quota"){
    viz.innerHTML = `<div class="qtrack"><div class="qfill ${step.trip?'cut':''}" data-w="${step.pct}"></div><div class="qpin"></div></div>`; applyDataStyles(viz);
  } else {
    viz.innerHTML = `<div class="xbars">${(step.bars||[]).map(b=>`
      <div class="xb"><div class="xb-h"><span>${b[0]}</span><b>${b[1]}</b></div><div class="xb-t"><div class="xb-f" data-w="${b[2]}" data-bg="${b[3]}"></div></div></div>
    `).join("")}</div>`; applyDataStyles(viz);
  }
}

function playMode(mode){
  timers.forEach(clearTimeout); timers = [];
  curMode = mode;
  const cfg = MODES[mode];
  streamEl.innerHTML = "";
  document.getElementById("hud-k").textContent = cfg.hudK;
  document.getElementById("hud-tag").textContent = cfg.tag;
  hudBtn.textContent = cfg.btn;
  hudBtn.classList.remove("ready");
  hudBtn.disabled = true;
  ledEl.className = "led";
  ledTxt.textContent = "RUNNING";

  cfg.steps.forEach((step, i) => {
    timers.push(setTimeout(() => {
      const el = document.createElement("div");
      if(step.cut){
        el.className = "cut-box";
        el.innerHTML = step.cut;
      } else {
        el.className = "tl";
        el.innerHTML = step.t;
        requestAnimationFrame(()=> el.classList.add("on"));
      }
      streamEl.appendChild(el);
      document.getElementById("hud-val").innerHTML = step.v;
      document.getElementById("hud-sub").textContent = step.sub;
      renderViz(mode, step);
      document.getElementById("hud-dl").innerHTML = step.kv.map(r =>
        `<div><span>${r[0]}</span><span class="${r[2]||''}">${r[1]}</span></div>`
      ).join("");
      if(step.trip){
        ledEl.className = "led " + cfg.ledDone[0];
        ledTxt.textContent = cfg.ledDone[1];
        hudBtn.classList.add("ready");
        hudBtn.disabled = false;
      }
    }, i * 380));
  });
}

document.querySelectorAll(".st-tab").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".st-tab").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    playMode(b.dataset.mode);
  });
});
document.getElementById("stage-replay").addEventListener("click", () => playMode(curMode));
hudBtn.addEventListener("click", () => {
  if(curMode === "waste"){
    navigator.clipboard?.writeText("pipx install agentcap-cli && agentcap waste --since 7d").catch(()=>{});
    hudBtn.textContent = "Copied!";
    setTimeout(()=> hudBtn.textContent = MODES.waste.btn, 1400);
  } else {
    const el = document.createElement("div");
    el.className = "tl on";
    el.innerHTML = '<span class="g">&gt; /agentcap:approve → limit raised +50% (1/2). Resuming…</span>';
    streamEl.appendChild(el);
    ledEl.className = "led";
    ledTxt.textContent = "RESUMED (+50%)";
    hudBtn.classList.remove("ready");
    hudBtn.disabled = true;
  }
});

document.getElementById("hero-copy").addEventListener("click", function(){
  navigator.clipboard?.writeText("/plugin marketplace add Blitz00X/agentcap\n/plugin install agentcap@agentcap").catch(()=>{});
  this.textContent = "Copied";
  setTimeout(()=> this.textContent = "Copy", 1400);
});

const INSTALLS = {
  claude: {
    code: `# Run inside Claude Code (no pip needed)
/plugin marketplace add Blitz00X/agentcap
/plugin install agentcap@agentcap

# Cap this session, or set a default for claude -p
/agentcap:quota 5% 5h`,
    copy: "/plugin marketplace add Blitz00X/agentcap\n/plugin install agentcap@agentcap"
  },
  waste: {
    code: `# Zero hooks · read-only scan of your local logs
pipx install agentcap-cli
agentcap waste --since 7d`,
    copy: "pipx install agentcap-cli && agentcap waste --since 7d"
  },
  codex: {
    code: `# Codex 0.167+ (Experimental)
codex plugin marketplace add Blitz00X/agentcap
codex plugin add agentcap@agentcap
# Type directly as a prompt (never sent to model):
agentcap 10% 5h`,
    copy: "codex plugin marketplace add Blitz00X/agentcap && codex plugin add agentcap@agentcap"
  },
  api: {
    code: `# Fail-closed localhost proxy for API keys
pipx install agentcap-cli
agentcap run --cap 2 -- claude -p "fix failing tests"`,
    copy: "pipx install agentcap-cli"
  }
};
let curInstall = "claude";
function setInstall(k){
  curInstall = k;
  document.getElementById("install-pre").textContent = INSTALLS[k].code;
}
document.querySelectorAll(".mt").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".mt").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    setInstall(b.dataset.t);
  });
});
document.getElementById("install-copy").addEventListener("click", function(){
  navigator.clipboard?.writeText(INSTALLS[curInstall].copy).catch(()=>{});
  this.textContent = "Copied";
  setTimeout(()=> this.textContent = "Copy", 1400);
});

(function(){
  const allowed=['utm_source','utm_medium','utm_campaign','utm_content','utm_term'];
  const buy=document.querySelector('a[href^="https://buy.polar.sh/"]');
  if(!buy) return;
  const inc=new URLSearchParams(location.search), out=new URL(buy.href);
  allowed.forEach(k=>{const v=inc.get(k); if(v) out.searchParams.set(k,v.slice(0,200));});
  buy.href=out.toString();
})();

setInstall("claude");
playMode("quota");

/* hero quota meter: fill to 30%, AgentCap stops it, repeat */
(function(){
  var m=document.getElementById("heroMeter"); if(!m) return;
  var f=document.getElementById("hmFill"), n=document.getElementById("hmNum");
  var LIMIT=30, RUN=1400, HOLD=3800, WAIT=700;
  var still=window.matchMedia&&window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function show(p){ f.style.width=p+"%"; n.textContent=Math.floor(p)+"%"; }
  if(still){ show(LIMIT); m.classList.add("hit"); return; }
  function cycle(first){
    m.classList.remove("hit"); var t0=null;
    function step(t){
      if(t0===null) t0=t;
      var k=Math.min((t-t0)/RUN,1);
      var p=LIMIT*k*k;
      show(p);
      if(k<1) requestAnimationFrame(step);
      else { show(LIMIT); m.classList.add("hit"); setTimeout(cycle,HOLD); }
    }
    show(0); setTimeout(function(){ requestAnimationFrame(step); },first?150:WAIT);
  }
  cycle(true);
})();
