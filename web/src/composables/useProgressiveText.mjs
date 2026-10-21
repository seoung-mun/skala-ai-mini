import { ref, watch, onMounted, onUnmounted } from "vue";

// Reveal only the final response; partial model output has not been verified.
export function useProgressiveText(text, enabled) {
  const visible = ref(enabled.value ? "" : text.value);
  const revealing = ref(false);
  let timer;
  function finish() {
    clearInterval(timer);
    timer = undefined;
    visible.value = text.value;
    revealing.value = false;
  }
  function start() {
    finish();
    if (
      !enabled.value ||
      !text.value ||
      globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches
    )
      return;
    const characters = Array.from(text.value);
    const duration = Math.min(4000, Math.max(400, characters.length * 8));
    const started = Date.now();
    visible.value = "";
    revealing.value = true;
    timer = setInterval(() => {
      const count = Math.ceil(
        (characters.length * (Date.now() - started)) / duration,
      );
      visible.value = characters.slice(0, count).join("");
      if (count >= characters.length) finish();
    }, 20);
  }
  onMounted(start);
  watch(text, start);
  onUnmounted(() => clearInterval(timer));
  return { visible, revealing, finish };
}
