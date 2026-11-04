let audio: AudioContext | undefined;

// Called from a user gesture so the WebView permits the completion sound later.
export function unlockChatSound() {
  try {
    audio ??= new AudioContext();
    void audio.resume().catch(() => undefined);
  } catch { /* Audio is optional when the browser has no output device. */ }
}

export function playChatSound() {
  if (!audio || audio.state !== "running") return;
  try {
    const oscillator = audio.createOscillator();
    const gain = audio.createGain();
    oscillator.connect(gain);
    gain.connect(audio.destination);
    const now = audio.currentTime;
    oscillator.frequency.setValueAtTime(660, now);
    oscillator.frequency.setValueAtTime(880, now + 0.12);
    gain.gain.setValueAtTime(0, now);
    gain.gain.linearRampToValueAtTime(0.09, now + 0.015);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.3);
    oscillator.start(now);
    oscillator.stop(now + 0.32);
    oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
  } catch { /* Notification failures must never hide a model response. */ }
}
