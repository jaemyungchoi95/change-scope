"use strict";
// Untrusted document text is only ever inserted with textContent.

const KIND_TITLES = {
  source_fact: "원문 사실",
  rule_conclusion: "규칙 결론",
  suggestion: "추가 제안 (미검토)",
  unverified: "미확인",
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function getJSON(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json();
  return { ok: response.ok, status: response.status, body };
}

function errorBox(body) {
  const error = body && body.error ? body.error : { kind: "unknown", message: "요청 실패" };
  return el("div", { class: "card error" }, `오류 [${error.kind}] ${error.message}`);
}

function status(value) {
  return el("span", { class: `status ${value}` }, value);
}

function highlight(text, quote) {
  const at = quote ? text.indexOf(quote) : -1;
  if (at < 0) return el("pre", {}, text);
  return el("pre", {}, text.slice(0, at), el("mark", {}, quote), text.slice(at + quote.length));
}

// ---------- ① review ----------
let reviewData = null;
let reviewFilter = "";

const STATUS_KO = { approved: "분석 반영", rejected: "반려", pending: "검토 대기", withdrawn: "철회" };
const CHECK_KO = {
  quote_missing: "인용한 문장이 원문 어디에도 없습니다",
  section_mismatch: "인용한 문장이 지정한 절이 아니라 다른 절에 있습니다",
  section_missing: "지정한 절이 원문에 없습니다",
  section_ambiguous: "같은 이름의 절이 여러 개라 위치를 특정할 수 없습니다",
  self_reference: "자기 자신을 호출한다는 관계라 의미가 없습니다",
};

function section(title, desc) {
  return [el("h3", { class: "section-title" }, title), el("p", { class: "desc" }, desc)];
}

function koStatus(value) {
  return el("span", { class: `status ${value}` }, STATUS_KO[value] || value);
}

function groupOf(c) {
  if (c.current_status === "approved") return "approved";
  if (c.current_status === "pending") return "todo";
  return "closed";
}

function why(c) {
  if (c.current_status === "approved") return "사람이 원문을 확인해 승인했고, 영향 분석에 사용됩니다.";
  if (c.current_status === "withdrawn") return "승인했다가 근거를 철회했습니다. 분석에서 빠집니다.";
  if (c.current_status === "rejected") {
    return c.decision ? `사람이 반려했습니다: ${c.decision.reason}` : "반려되었습니다.";
  }
  if (!c.source_current) return `원문이 v${reviewData.lifecycle.current_source_version}로 바뀌었는데 v${c.source_version} 기준 후보입니다. 새 원문으로 수정(revise)한 뒤 다시 판정해야 합니다.`;
  if (c.source_check.status === "failed") return `기계 검사 실패: ${CHECK_KO[c.source_check.kind] || c.source_check.kind}. 승인할 수 없고 반려만 가능합니다.`;
  return "기계 검사는 통과했고 사람의 판정을 기다립니다.";
}

function flow() {
  const x = reviewData.extraction;
  const origin = x.llm_mode === "recorded_output" ? "기록된 AI 추출 출력(합성 시험용)" : "AI 추출";
  const steps = [
    ["원문 문서", `${reviewData.source.source_id} 문서 (현재 v${reviewData.lifecycle.current_source_version})`],
    ["AI 추출 후보", `${origin}이 "A가 B를 HTTP로 호출한다"는 관계와 근거 문장을 제안`],
    ["기계 검사", "근거 문장이 지정한 절에 실제로 있는지만 확인 (의미·방향은 확인 안 함)"],
    ["사람 판정", "원문을 읽고 승인 / 반려 / 철회. 이유와 함께 기록"],
    ["영향 분석", "승인된 관계만 ② 화면 계산에 사용"],
  ];
  return el(
    "div",
    { class: "card" },
    el("h3", {}, "후보는 어떻게 만들어지나요?"),
    el("p", { class: "desc" }, "여기서 '후보'는 서비스 사이의 호출 관계입니다. AI가 문서에서 찾아 제안하고, 사람이 승인한 것만 분석에 쓰입니다."),
    el("ol", { class: "flow" }, steps.map(([title, text]) => el("li", {}, el("strong", {}, title), el("span", {}, text)))),
  );
}

function tiles() {
  const s = reviewData.summary;
  const life = reviewData.lifecycle;
  const items = [
    ["", "전체 후보", s.total, "필터 해제"],
    ["approved", "분석에 반영", s.approved, "승인되어 계산에 쓰이는 관계"],
    ["pending", "검토 대기", s.pending, "판정 전이거나 원문이 바뀌어 다시 봐야 함"],
    ["rejected", "반려", s.rejected, "방향이 틀렸거나 근거가 없는 후보"],
    ["withdrawn", "철회", life.withdrawn, "승인 후 근거를 거둬들임"],
  ];
  return items.map(([key, label, count, hint]) =>
    el(
      "button",
      { type: "button", class: `tile ${reviewFilter === key ? "on" : ""}`, onclick: () => { reviewFilter = key; renderCandidates(); } },
      el("span", { class: "tile-count" }, String(count)),
      el("span", { class: "tile-label" }, label),
      el("span", { class: "tile-hint" }, hint),
    ),
  );
}

function candidateRow(c) {
  return el(
    "button",
    { type: "button", class: "cand", "data-id": c.candidate_id, onclick: () => showCandidate(c.candidate_id) },
    el("div", { class: "cand-head" }, el("strong", {}, `${c.subject} → ${c.object}`), " 호출 ", koStatus(c.current_status)),
    el("div", { class: "cand-quote" }, `근거 문장: "${c.quote}"`, el("span", { class: "muted" }, ` (${c.section} 절, 원문 v${c.source_version})`)),
    el("div", { class: "cand-why" }, why(c)),
    el("div", { class: "cand-id" }, `${c.candidate_id} · 수정 ${c.revision}회차`),
  );
}

async function loadReview() {
  const { ok, body } = await getJSON("/api/review");
  if (!ok) {
    document.getElementById("candidate-groups").replaceChildren(errorBox(body));
    return;
  }
  reviewData = body;
  document.getElementById("review-flow").replaceChildren(flow());
  renderCandidates();
}

function renderCandidates() {
  document.getElementById("review-tiles").replaceChildren(...tiles());
  const shown = reviewData.candidates.filter((c) => !reviewFilter || c.current_status === reviewFilter);
  const groups = [
    ["approved", "✅ 분석에 반영된 관계", "사람이 근거 원문을 확인하고 승인한 호출 관계입니다. ② 영향 화면은 이 관계만으로 계산합니다."],
    ["todo", "⏸ 다시 검토가 필요한 후보", "아직 판정하지 않았거나, 원문이 바뀌어 예전 판정을 그대로 쓸 수 없는 후보입니다. 분석에는 쓰이지 않습니다."],
    ["closed", "❌ 반려·철회된 후보", "방향이 틀렸거나 근거가 없어 제외한 후보입니다. 왜 뺐는지 기록이 남아 있습니다."],
  ];
  const parts = groups.flatMap(([key, title, desc]) => {
    const items = shown.filter((c) => groupOf(c) === key);
    if (!items.length) return [];
    return [section(`${title} (${items.length})`, desc), el("div", { class: "cand-list" }, items.map(candidateRow))];
  });
  document.getElementById("candidate-groups").replaceChildren(...(parts.length ? parts : [el("p", { class: "muted" }, "해당하는 후보가 없습니다.")]).flat(Infinity).filter(Boolean));
}

async function showCandidate(id) {
  document.querySelectorAll(".cand").forEach((b) => b.classList.toggle("sel", b.dataset.id === id));
  const detail = document.getElementById("candidate-detail");
  const { ok, body } = await getJSON(`/api/review?id=${encodeURIComponent(id)}`);
  if (!ok) return detail.replaceChildren(errorBox(body));
  const c = body.candidates[0];
  const decision = c.decision;
  const check = c.source_check.status === "passed"
    ? el("p", { class: "ok" }, "✔ 근거 문장이 지정한 절에 실제로 있습니다.")
    : el("p", { class: "bad" }, `✖ ${CHECK_KO[c.source_check.kind] || c.source_check.kind}`);
  detail.replaceChildren(
    ...[
      el("h3", {}, `${c.subject} → ${c.object} 호출 `, koStatus(c.current_status)),
      el("p", { class: "desc" }, why(c)),
      section("1. 근거 원문", `원문 ${c.source_id} v${c.source_version}의 "${c.section}" 절입니다. 노란 부분이 AI가 근거로 든 문장입니다.`),
      c.section_text ? highlight(c.section_text, c.quote) : el("p", { class: "bad" }, "지정한 절을 원문에서 하나로 특정할 수 없습니다."),
      section("2. 기계 검사", "근거 문장의 위치만 확인합니다. 관계의 의미와 방향이 맞는지는 사람이 판단합니다."),
      check,
      section("3. 사람 판정", "누가, 언제, 어떤 이유로 판정했는지입니다."),
      decision
        ? el("p", {}, `${decision.decision === "approve" ? "승인" : "반려"} · ${decision.reviewer} · ${decision.decided_at.slice(0, 16).replace("T", " ")}`, el("br"), `이유: ${decision.reason}`)
        : el("p", { class: "muted" }, c.approvable ? "아직 판정하지 않았습니다. (CLI: review decide)" : "이 상태로는 승인할 수 없습니다. 반려하거나 새 원문으로 수정해야 합니다."),
      section("4. 수정 이력", "같은 후보가 원문 변경이나 정정으로 다시 제출된 기록입니다. 예전 판정은 새 수정본에 넘어가지 않습니다."),
      el(
        "ol",
        { class: "timeline" },
        (c.history || []).map((h) =>
          el("li", {}, `${h.revision}회차 · 원문 v${h.source_version} · ${h.subject} → ${h.object} `, koStatus(h.status), h.decision ? el("div", { class: "muted" }, `${h.decision.reviewer}: ${h.decision.reason}`) : null),
        ),
      ),
      el("p", { class: "cand-id" }, `후보 ID ${c.candidate_id}`),
    ].flat(Infinity),
  );
}

// ---------- ② impact ----------
let config = null;

function hopLines(witness) {
  return witness.hops.map((hop) =>
    el(
      "div",
      { class: "hop" },
      `${hop.subject} → ${hop.object}: `,
      hop.support.map((s, i) => [
        i ? " / " : "",
        el("code", {}, s.fact_id),
        s.evidence_kind === "proposal" ? " (변경 제안)" : "",
        ` "${s.quote}"`,
      ]),
    ),
  );
}

function evidenceList(found) {
  if (!found) return [];
  const items = [
    ...found.tests.map((t) => el("li", {}, "테스트 ", el("code", {}, t.doc_id), ` ${t.title}`)),
    ...found.documents.map((d) => el("li", {}, "문서 ", el("code", {}, d.doc_id), ` ${d.title}`)),
  ];
  return items.length ? el("ul", {}, items) : el("p", { class: "warn" }, "연결된 테스트·문서 없음 (미확인)");
}

// Human-readable labels for evidence IDs (facts, links, documents).
function labelsFor(result) {
  const labels = {};
  for (const fact of result.facts || []) {
    for (const s of fact.support) {
      const id = s.assertion_id || s.candidate_id;
      labels[id] = `${fact.subject} → ${fact.object} 호출 ("${s.quote}")`;
    }
  }
  const groups = [result.target_checks, ...Object.values(result.candidate_evidence || {})];
  for (const found of groups) {
    if (!found) continue;
    for (const link of [...found.tests, ...found.documents]) {
      labels[link.doc_id] = `${link.relation === "about" ? "문서" : "테스트"} "${link.title}"`;
      labels[link.link_id] = `${link.title}에 ${link.service} 언급`;
    }
  }
  return labels;
}

function reason(target, witness) {
  const path = witness.path;
  if (path.length === 2) return `${path[0]}이(가) ${target}을(를) 직접 호출합니다.`;
  return `${path.join(" → ")} 순서로 호출이 이어져 ${target} 변경의 영향을 받을 수 있습니다.`;
}

function testsCell(found) {
  if (!found) return el("span", { class: "muted" }, "(evidence 미지정)");
  if (!found.tests.length) return el("span", { class: "bad" }, "⚠ 연결된 기존 테스트 없음 → 테스트 보강 필요 (미확인)");
  return el("ul", { class: "plain" }, found.tests.map((t) => el("li", {}, `▶ ${t.title} `, el("code", {}, t.doc_id))));
}

function docsCell(found) {
  if (!found || !found.documents.length) return el("span", { class: "muted" }, "-");
  return el("ul", { class: "plain" }, found.documents.map((d) => el("li", {}, `${d.title} `, el("code", {}, d.doc_id))));
}

function checklist(result) {
  const rows = [
    el(
      "tr",
      { class: "target-row" },
      el("td", {}, el("strong", {}, result.target), el("div", { class: "muted" }, "변경 대상 자체")),
      el("td", {}, "변경하는 서비스 자신의 동작을 확인합니다."),
      el("td", {}, testsCell(result.target_checks)),
      el("td", {}, docsCell(result.target_checks)),
    ),
    ...result.upstream_candidates.map((s) => {
      const paths = result.witnesses[s] || [];
      const found = result.candidate_evidence ? result.candidate_evidence[s] : null;
      return el(
        "tr",
        {},
        el("td", {}, el("strong", {}, s), paths.length > 1 ? el("div", { class: "muted" }, `영향 경로 ${paths.length}개`) : null),
        el("td", {}, el("ul", { class: "plain" }, paths.map((w) => el("li", {}, reason(result.target, w))))),
        el("td", {}, testsCell(found)),
        el("td", {}, docsCell(found)),
      );
    }),
  ];
  return el(
    "table",
    { class: "checklist" },
    el("thead", {}, el("tr", {}, el("th", {}, "서비스"), el("th", {}, "왜 점검하나"), el("th", {}, "실행할 기존 테스트"), el("th", {}, "참고 문서"))),
    el("tbody", {}, rows),
  );
}

function headline(result) {
  const n = result.upstream_candidates.length;
  if (!n) return `${result.target}을(를) 호출하는 상위 서비스가 없습니다. 변경 대상 자체만 확인하면 됩니다.`;
  return `${result.target}을(를) 변경하면 ${n}개 서비스(${result.upstream_candidates.join(", ")})를 함께 다시 점검해야 합니다.`;
}

function cautions(result) {
  const items = [];
  const groups = [[result.target, result.target_checks], ...result.upstream_candidates.map((s) => [s, result.candidate_evidence && result.candidate_evidence[s]])];
  const untested = groups.filter(([, found]) => found && !found.tests.length).map(([s]) => s);
  if (untested.length) items.push(`기존 테스트가 연결되지 않은 서비스: ${untested.join(", ")}. 점검 방법을 따로 정해야 합니다.`);
  items.push("이 결과는 사람이 검토한 문서 관계로 계산했습니다. 실제 배포 구성과 같은지는 확인되지 않았습니다.");
  if (result.candidate_evidence) items.push("테스트가 연결되어 있다는 것은 그 테스트가 서비스를 다룬다는 뜻일 뿐, 충분하다는 보장은 아닙니다.");
  return el("ul", { class: "cautions" }, items.map((t) => el("li", {}, t)));
}

function suggestionPanel(result) {
  const items = (result.claims || []).filter((c) => c.kind === "suggestion");
  if (!items.length) {
    if (!config || config.llm === "off" || !result.claims) return null;
    return el(
      "div",
      { class: "card suggest-panel idle" },
      el("h3", {}, "AI 점검 제안 (선택)"),
      el("p", { class: "desc" }, `위쪽 "LLM 제안 생성" 버튼을 누르면 AI가 이 화면의 근거만 보고 추가로 확인할 점을 제안합니다. 결과는 '검토 전' 제안으로만 표시되고 체크리스트에 자동 반영되지 않습니다.`),
    );
  }
  const labels = labelsFor(result);
  const live = items[0].llm_mode === "live_cli";
  return el(
    "div",
    { class: "card suggest-panel" },
    el("h3", {}, `AI 점검 제안 ${items.length}건 `, el("span", { class: "status pending" }, "검토 전"), " ", el("span", { class: "muted" }, live ? "실제 Codex 호출 결과" : "기록된 출력 재생")),
    el("p", { class: "muted" }, "AI가 위 근거만 보고 제안한 내용입니다. 사실로 확정되거나 테스트 목록에 자동 반영되지 않습니다."),
    el(
      "ol",
      {},
      items.map((c) =>
        el(
          "li",
          {},
          el("span", { class: "svc" }, c.service),
          " ",
          c.text,
          el("div", { class: "refs" }, "근거: ", c.refs.map((r, i) => [i ? " · " : "", labels[r] || r])),
        ),
      ),
    ),
  );
}

function details(result) {
  const parts = [
    el(
      "div",
      { class: "cards" },
      result.upstream_candidates.map((s) =>
        el(
          "div",
          { class: "card" },
          el("h3", {}, s),
          (result.witnesses[s] || []).map((w) => [el("div", { class: "path" }, `경로(${w.via} 경유): ${w.path.join(" → ")}`), hopLines(w)]),
        ),
      ),
    ),
  ];
  if (result.claims) {
    for (const [kind, label] of Object.entries(KIND_TITLES)) {
      const items = result.claims.filter((c) => c.kind === kind);
      parts.push(
        el("h4", {}, `${label} (${items.length})`),
        el(
          "ul",
          { class: "claims" },
          items.map((c) => el("li", {}, c.text, c.refs && c.refs.length ? [" [", c.refs.join(", "), "]"] : "", c.quote ? ` — "${c.quote}"` : "")),
        ),
      );
    }
  }
  return el(
    "details",
    { class: "details" },
    el("summary", {}, "상세 근거 보기: 호출 경로별 원문 인용 · 주장 분류(원문 사실/규칙 결론/제안/미확인) · 실행 정보"),
    el("p", { class: "muted" }, `${result.snapshot_id} · ${result.snapshot_kind || "fixture"} · 엔진 ${result.engine.name} ${result.engine.version} · rules ${result.rules_hash.slice(0, 12)}`),
    parts,
  );
}

function renderAnalysis(result) {
  return [
    el(
      "div",
      { class: "card headline" },
      el("p", { class: "desc" }, `결론 · ${result.target}을(를) 바꿀 때 함께 확인해야 하는 범위`),
      el("h2", {}, headline(result)),
      result.analysis_status === "empty" ? el("p", { class: "muted" }, "정상적인 빈 결과입니다(오류 아님).") : null,
    ),
    suggestionPanel(result),
    section("점검 체크리스트", "변경 대상과, 호출 경로를 따라 영향을 받을 수 있는 서비스를 한 줄씩 정리했습니다. 위에서부터 기존 테스트를 실행하고, ⚠ 표시가 있는 서비스는 점검 방법을 새로 정해야 합니다."),
    checklist(result),
    section("주의할 점", "결과를 그대로 믿기 전에 알아야 할 한계입니다."),
    cautions(result),
    details(result),
  ];
}

function renderComparison(body) {
  const c = body.comparison;
  const applied = c.overrides.map((a) => `${a.proposal_id}(${a.subject} → ${a.object} ${a.operation === "remove" ? "호출 제거" : "호출 추가"})`).join(", ");
  const lines = [
    `${applied}을(를) 반영하면 ${c.target} 변경 시 점검 대상이 ${c.base.upstream_candidates.length}개 → ${c.scenario.upstream_candidates.length}개가 됩니다.`,
  ];
  if (c.diff.removed.length) lines.push(`${c.diff.removed.join(", ")}은(는) 더 이상 ${c.target}에 닿는 호출 경로가 없어 점검 대상에서 빠집니다.`);
  if (c.diff.added.length) lines.push(`${c.diff.added.join(", ")}이(가) 새로 점검 대상이 됩니다.`);
  for (const [s, change] of Object.entries(c.path_changes)) {
    lines.push(`${s}은(는) 경로가 바뀌어도 ${change.scenario.map((p) => p.join(" → ")).join(", ")} 경로로 여전히 영향을 받습니다.`);
  }
  return [
    el(
      "div",
      { class: "card headline" },
      el("h2", {}, "변경안 반영 시 달라지는 점"),
      el("p", { class: "desc" }, "현재 기준(S1)과, 승인된 변경 제안을 반영한 시나리오(S2)를 같은 엔진으로 계산해 비교했습니다."),
      el("ul", {}, lines.map((t) => el("li", {}, t))),
      c.not_applied.length ? el("p", { class: "muted" }, `적용되지 않은 제안: ${c.not_applied.map((n) => `${n.proposal_id}(${n.status === "rejected" ? "반려" : "검토 대기"})`).join(", ")}`) : null,
    ),
    section("변경안 반영 후 점검 체크리스트", `승인된 변경안을 반영한 시나리오(${c.scenario.snapshot_id}) 기준으로 다시 계산한 점검 목록입니다.`),
    checklist(body.scenario_result),
    section("주의할 점", "결과를 그대로 믿기 전에 알아야 할 한계입니다."),
    cautions(body.scenario_result),
    el("details", { class: "details" }, el("summary", {}, "현재 기준(S1) 결과 보기"), renderAnalysis(body.base_result)),
    details(body.scenario_result),
  ];
}

async function loadImpact() {
  const target = document.getElementById("target").value;
  const compare = document.getElementById("compare-toggle").checked;
  const statusLine = document.getElementById("impact-status");
  const box = document.getElementById("impact");
  statusLine.textContent = "실제 엔진으로 계산 중…";
  const url = compare ? `/api/compare?target=${target}` : `/api/analysis?target=${target}`;
  const { ok, body } = await getJSON(url);
  statusLine.textContent = "";
  if (!ok) return box.replaceChildren(errorBox(body));
  box.replaceChildren(...(compare ? renderComparison(body) : renderAnalysis(body)).flat(Infinity).filter(Boolean));
}

async function requestSuggestions() {
  const target = document.getElementById("target").value;
  const live = config.llm === "live";
  if (live && !confirm(`실제 Codex 구독 호출을 1회 실행합니다 (target=${target}). 계속할까요?`)) return;
  const button = document.getElementById("suggest-btn");
  const statusLine = document.getElementById("impact-status");
  button.disabled = true;
  statusLine.textContent = live ? "Codex 호출 중… (최대 2분)" : "기록된 제안 재생 중…";
  try {
    const { ok, body } = await getJSON("/api/suggestions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-ChangeScope-Action": "suggest" },
      body: JSON.stringify({ target }),
    });
    const box = document.getElementById("impact");
    document.getElementById("compare-toggle").checked = false;
    if (!ok) return box.replaceChildren(errorBox(body));
    box.replaceChildren(...renderAnalysis(body).flat(Infinity).filter(Boolean));
    box.querySelector(".suggest-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  } finally {
    button.disabled = false;
    statusLine.textContent = "";
  }
}

function switchView(view) {
  for (const button of document.querySelectorAll(".tabs button")) {
    button.setAttribute("aria-selected", String(button.dataset.view === view));
  }
  document.getElementById("view-review").hidden = view !== "review";
  document.getElementById("view-impact").hidden = view !== "impact";
  if (view === "impact" && !document.getElementById("impact").childElementCount) loadImpact();
}

async function init() {
  config = (await getJSON("/api/config")).body;
  document.getElementById("snapshot-badge").textContent = `snapshot ${config.snapshot}`;
  document.getElementById("llm-badge").textContent = `LLM ${config.llm}`;
  const target = document.getElementById("target");
  target.replaceChildren(...config.services.map((s) => el("option", { value: s }, s)));
  target.value = "reranker";
  document.getElementById("compare-toggle").disabled = !config.has_scenario;
  document.getElementById("suggest-btn").disabled = config.llm === "off" || !config.has_evidence;
  if (config.llm === "live") document.getElementById("suggest-btn").textContent = "LLM 제안 생성 (실제 호출)";
  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => switchView(button.dataset.view));
  }
  target.addEventListener("change", loadImpact);
  document.getElementById("compare-toggle").addEventListener("change", loadImpact);
  document.getElementById("suggest-btn").addEventListener("click", requestSuggestions);
  if (config.has_workspace) loadReview();
  else switchView("impact");
}

init();
