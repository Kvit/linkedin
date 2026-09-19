// The message box of the Contact and Suggested screens: the character count,
// the AI button in place (Expand or Rework, with Undo), Cancel back to the
// saved draft, a confirm before Clear, and buttons disabled once the form is
// sent (after the submitter's value is taken, so "Cancel it and send mine"
// still posts its item).
(() => {
  const form = document.querySelector(".compose-form");
  if (!form) return;
  const box = form.elements.text;
  const instructions = form.elements.instructions;  // Suggested screen only
  const count = form.querySelector("[data-count]");
  const status = form.querySelector("[data-status]");
  const ai = form.querySelector("[data-ai-button]");
  const limit = Number(form.dataset.limit);
  const recount = () => {
    count.textContent = box.value.length;
    count.parentElement.classList.toggle("over", box.value.length > limit);
  };
  const say = (text, undo) => {
    status.hidden = false;
    status.textContent = text;
    if (undo === undefined) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "link";
    button.textContent = "Undo";
    button.addEventListener("click", () => { box.value = undo; recount(); status.hidden = true; box.focus(); });
    status.append(" ", button);
  };
  box.addEventListener("input", recount);
  recount();
  const write = async () => {
    const text = box.value;
    if (!text.trim()) { say(form.dataset.aiEmpty); return; }
    ai.disabled = true;
    say(form.dataset.aiBusy);
    try {
      const body = instructions ? {text, instructions: instructions.value} : {text};
      const response = await fetch(form.dataset.ai, {
        method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body),
      });
      const answer = response.ok ? await response.json() : {ok: false, detail: ai.textContent + " failed: HTTP " + response.status + "."};
      if (!answer.ok) { say(answer.detail); return; }
      box.value = answer.text;
      recount();
      say("Written by " + answer.model + " in " + answer.seconds + " s." + (answer.problem ? " " + answer.problem : ""), text);
    } catch (error) {
      say(ai.textContent + " failed: " + error + ".");
    } finally {
      ai.disabled = false;
    }
  };
  ai.addEventListener("click", write);
  // Enter in the instructions runs Rework instead of submitting the form.
  instructions?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); write(); }
  });
  form.querySelector("[data-cancel]")?.addEventListener("click", () => {
    box.value = box.dataset.saved ?? "";
    recount();
    status.hidden = true;
    box.focus();
  });
  form.addEventListener("submit", (event) => {
    const question = event.submitter?.dataset.confirm;
    if (question && !window.confirm(question)) { event.preventDefault(); return; }
    setTimeout(() => form.querySelectorAll("button").forEach((button) => { button.disabled = true; }), 0);
  });
})();
