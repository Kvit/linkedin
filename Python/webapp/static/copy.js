// Copy buttons: <button type="button" data-copy="the text">Copy</button>.
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  try {
    await navigator.clipboard.writeText(button.dataset.copy);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Copy failed";
  }
  setTimeout(() => { button.textContent = "Copy"; }, 2000);
});
