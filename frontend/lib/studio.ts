export type View = "dashboard" | "generator" | "script" | "storyboard" | "editor" | "characters" | "library" | "settings";
export type Scene = { id: string; prompt: string; narration: string; duration: number; trim_start?: number; trim_end?: number; asset_id?: string; status?: string; voice?: string; camera?: string; sound_effect?: string };
export type ProjectSettings = { resolution: string; aspect_ratio: string; duration: number; fps: number; style: string; camera: string; quality: string; seed?: number; image_asset_id?: string; reference_asset_id?: string; narration_enabled?: boolean; voice?: string; subtitles?: boolean; [key: string]: unknown };
export type Project = { id?: string; title: string; prompt: string; mode: "text" | "image" | "script"; language: string; provider: string; settings: ProjectSettings; scenes: Scene[]; output_asset_id?: string | null; created_at?: string; updated_at?: string; metadata?: Record<string, unknown> };
export type Provider = { id: string; name: string; configured: boolean; available: boolean; reason?: string; modes: string[]; resolutions: string[]; aspect_ratios: string[]; durations: number[]; fps: number[] | number; cost_note: string; limitations: string[] | string };
export type Job = { id: string; project_id: string; status: string; stage: string; progress: number; error?: string; logs: { time: string; message: string }[]; output_asset_id?: string; created_at: string; updated_at: string; cancel_requested?: boolean };
export type Asset = { id: string; url?: string; name: string; kind: string; metadata?: Record<string, unknown> };
export type Character = { id?: string; name: string; age_range: string; appearance: string; clothing: string; personality: string; voice: string; reference_asset_id?: string };
export type Settings = { fal_configured?: boolean; llm_configured?: boolean; tts_configured?: boolean; comfyui_configured?: boolean; comfyui_url?: string; comfyui_workflow_path?: string; llm_base_url?: string; llm_model?: string; tts_base_url?: string; tts_model?: string; tts_voice?: string; youtube_enabled?: boolean; [key: string]: unknown };
export type Hardware = { gpus?: { name: string; memory_total_mb: number; memory_free_mb: number; driver: string }[]; ram_total_gb?: number; ram_available_gb?: number; disk_free_gb?: number; cpu_percent?: number; local_generation_note?: string };
export type Health = { status?: string; database?: string; worker?: { alive: boolean; last_seen?: string }; ffmpeg_available?: boolean };
export const VIDEO_LENGTHS = [45, 60, 90, 120];
export const newProject = (): Project => ({ title: "Untitled story", prompt: "", mode: "text", language: "Hindi", provider: "fal", settings: { resolution: "720p", aspect_ratio: "9:16", duration: 45, fps: 16, style: "Cinematic", camera: "Tracking", quality: "Balanced", narration_enabled: false, subtitles: false }, scenes: [] });
export const fileUrl = (id?: string | null) => id ? `/api/assets/${encodeURIComponent(id)}/file` : "";
export const activeJob = (j: Job) => ["queued", "running"].includes(j.status);
export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { cache: "no-store", ...options, headers: { ...(options?.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...options?.headers } });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try { const data = await response.json(); message = typeof data.detail === "string" ? data.detail : Array.isArray(data.detail) ? data.detail.map((x: {msg: string}) => x.msg).join("; ") : data.message || message; } catch { if (response.status === 502) message = "The studio server is offline. Start the backend and try again."; }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const json = (value: unknown) => JSON.stringify(value);
export const shortDate = (value?: string) => value ? new Date(value).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "Unsaved draft";

