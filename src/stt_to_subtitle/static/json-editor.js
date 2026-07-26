document.addEventListener("DOMContentLoaded", () => {
  const form = document.querySelector("[data-json-editor]");
  if (!form) return;

  const editor = form.querySelector("[data-json-content]");
  const formatButton = form.querySelector("[data-format-json]");
  const status = form.querySelector("[data-editor-status]");

  const showStatus = (message, invalid = false) => {
    status.textContent = message;
    status.classList.toggle("error-text", invalid);
  };

  const formatJSON = () => {
    try {
      editor.value = `${JSON.stringify(JSON.parse(editor.value), null, 2)}\n`;
      showStatus("JSON 문법이 올바릅니다.");
      return true;
    } catch (error) {
      showStatus(`JSON 문법 오류: ${error.message}`, true);
      return false;
    }
  };

  formatButton.addEventListener("click", formatJSON);
  editor.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    editor.setRangeText("  ", start, end, "end");
  });
  form.addEventListener("submit", (event) => {
    if (!formatJSON()) event.preventDefault();
  });
});
