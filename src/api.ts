export type ApiHealth = {
  status: string;
  database_ready: boolean;
  mode: string;
};

export type SourceVideo = {
  id: string;
  source_type: string;
  source_uri: string;
  title: string | null;
  created_at: string;
  local_path: string | null;
  duration_seconds: number | null;
  metadata_json: string | null;
};

export type VideoPreparation = {
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  frame_rate: number | null;
  video_codec: string | null;
  audio_available: boolean;
  audio_codecs: (string | null)[];
  format_name: string | null;
  readable: boolean;
  preparation_message: string;
};

export type Project = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
  source_videos: SourceVideo[];
};

export type Clip = {
  id: string;
  project_id: string;
  file_path: string | null;
  thumbnail_path: string | null;
  duration_seconds: number | null;
  created_at: string;
};

export type ProcessingJob = {
  id: string;
  project_id: string;
  project_name: string;
  status: string;
  progress_percent: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  job_type: string;
  stage: string | null;
  model_name: string | null;
  device: string | null;
  compute_type: string | null;
};

export type TranscriptionJob = {
  id: string;
  project_id: string;
  status: "queued" | "processing" | "completed" | "failed" | "cancelled";
  stage: string | null;
  error_message: string | null;
  model_name: string | null;
  device: string | null;
  compute_type: string | null;
  language: string | null;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string;
};

export type TranscriptResult = {
  metadata: {
    project_id: string;
    source_filename: string;
    language: string | null;
    language_probability: number | null;
    model: string;
    device: string;
    compute_type: string;
    processed_at: string;
    duration_seconds: number | null;
  };
  segments: { start: number; end: number; text: string; words: { start: number; end: number; text: string; probability?: number }[] }[];
  words: { start: number; end: number; text: string; probability?: number }[];
};

export type GeminiSettings = { configured: boolean; model: string; key_environment_variable: string };
export type HighlightJob = { id: string; project_id: string; status: string; stage: string | null; error_message: string | null; model_name: string | null; started_at: string | null; completed_at: string | null; updated_at: string };
export type HighlightCandidate = { id: string; project_id: string; transcript_id: string | null; start_seconds: number; end_seconds: number; duration_seconds: number | null; excerpt: string | null; title: string | null; hook: string | null; category: string | null; selection_explanation: string | null; score: number; hook_strength: number | null; engagement_potential: number | null; standalone_clarity: number | null; emotional_value: number | null; review_status: string; selected_for_render: boolean; validation_warnings: string | null; created_at: string };

const API_BASE_URL = "http://127.0.0.1:8765";

export async function getApiHealth(): Promise<ApiHealth> {
  const response = await fetch(`${API_BASE_URL}/api/health`);
  if (!response.ok) {
    throw new Error(`API health check failed (${response.status})`);
  }
  return response.json() as Promise<ApiHealth>;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    throw new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export const getProjects = () => request<Project[]>("/api/projects");
export const getProject = (id: string) => request<Project>(`/api/projects/${encodeURIComponent(id)}`);
export const deleteProject = (id: string) => request<{ deleted: boolean; cleanup_pending: boolean }>(`/api/projects/${encodeURIComponent(id)}`, { method: "DELETE" });
export const getClips = () => request<Clip[]>("/api/clips");
export const getJobs = () => request<ProcessingJob[]>("/api/jobs");
export const getLatestTranscriptionJob = (projectId: string) => request<TranscriptionJob>(`/api/projects/${encodeURIComponent(projectId)}/transcription`);
export const getTranscriptionJob = (projectId: string, jobId: string) => request<TranscriptionJob>(`/api/projects/${encodeURIComponent(projectId)}/transcription/jobs/${encodeURIComponent(jobId)}`);
export const startTranscription = (projectId: string, regenerate: boolean) => request<TranscriptionJob>(`/api/projects/${encodeURIComponent(projectId)}/transcription`, { method: "POST", body: JSON.stringify({ regenerate }) });
export const getTranscript = (projectId: string) => request<TranscriptResult>(`/api/projects/${encodeURIComponent(projectId)}/transcript`);
export const getTranscriptFolder = (projectId: string) => request<{ path: string }>(`/api/projects/${encodeURIComponent(projectId)}/transcript-folder`);
export const getGeminiSettings = () => request<GeminiSettings>("/api/settings/gemini");
export const startHighlightAnalysis = (projectId: string) => request<HighlightJob>(`/api/projects/${encodeURIComponent(projectId)}/highlights`, { method: "POST" });
export const getHighlightJob = (projectId: string, jobId?: string) => request<HighlightJob>(`/api/projects/${encodeURIComponent(projectId)}/highlights/jobs/${jobId ? encodeURIComponent(jobId) : "latest"}`);
export const getHighlightCandidates = (projectId: string) => request<HighlightCandidate[]>(`/api/projects/${encodeURIComponent(projectId)}/highlights/candidates`);
export const sourceVideoPreviewUrl = (projectId: string) => `${API_BASE_URL}/api/projects/${encodeURIComponent(projectId)}/source-video/preview`;
export const updateHighlightCandidate = (projectId: string, candidateId: string, data: Pick<HighlightCandidate, "start_seconds" | "end_seconds" | "title" | "hook">) => request<HighlightCandidate>(`/api/projects/${encodeURIComponent(projectId)}/highlights/candidates/${encodeURIComponent(candidateId)}`, { method: "PATCH", body: JSON.stringify(data) });
export const reviewHighlightCandidate = (projectId: string, candidateId: string, status: "approved" | "rejected") => request<HighlightCandidate>(`/api/projects/${encodeURIComponent(projectId)}/highlights/candidates/${encodeURIComponent(candidateId)}/review`, { method: "PATCH", body: JSON.stringify({ status }) });
export const selectHighlightCandidate = (projectId: string, candidateId: string, selected: boolean) => request<HighlightCandidate>(`/api/projects/${encodeURIComponent(projectId)}/highlights/candidates/${encodeURIComponent(candidateId)}/selection`, { method: "PATCH", body: JSON.stringify({ selected }) });
export const transcriptDownloadUrl = (projectId: string, format: "txt" | "srt" | "json") => `${API_BASE_URL}/api/projects/${encodeURIComponent(projectId)}/transcript/${format}`;
export const createProject = (input: {
  name: string;
  description?: string;
  video_url?: string;
}) => request<Project>("/api/projects", { method: "POST", body: JSON.stringify(input) });

export function uploadSourceVideo(projectId: string, file: File, onProgress: (percent: number) => void) {
  return new Promise<{ source_video: SourceVideo; preparation: VideoPreparation; size_bytes: number }>((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `${API_BASE_URL}/api/projects/${encodeURIComponent(projectId)}/source-video`);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    request.onerror = () => reject(new Error("Upload failed. Check that the local API is running and try again."));
    request.onload = () => {
      let body: any;
      try { body = JSON.parse(request.responseText); } catch { body = null; }
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(typeof body?.detail === "string" ? body.detail : `Upload failed (${request.status}).`));
      } else if (!body) reject(new Error("The API returned an invalid preparation report."));
      else resolve(body);
    };
    const form = new FormData();
    form.append("file", file, file.name);
    request.send(form);
  });
}
