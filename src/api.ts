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
};

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
export const getClips = () => request<Clip[]>("/api/clips");
export const getJobs = () => request<ProcessingJob[]>("/api/jobs");
export const createProject = (input: {
  name: string;
  description?: string;
  video_url?: string;
  source_video_name?: string;
}) => request<Project>("/api/projects", { method: "POST", body: JSON.stringify(input) });
