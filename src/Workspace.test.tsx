import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const project = {
  id: "p1", name: "Test project", description: "Demo", status: "created",
  created_at: "2026-01-01T00:00:00Z", source_videos: [],
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function mockUpload(status: number, body: unknown) {
  class MockUploadRequest {
    upload = { onprogress: null as ((event: ProgressEvent) => void) | null };
    status = status;
    responseText = JSON.stringify(body);
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    open() {}
    send() {
      this.upload.onprogress?.({ lengthComputable: true, loaded: 100, total: 100 } as ProgressEvent);
      queueMicrotask(() => this.onload?.());
    }
  }
  vi.stubGlobal("XMLHttpRequest", MockUploadRequest);
}

describe("application interface", () => {
  beforeEach(() => {
    cleanup();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects") && init?.method === "POST") return response(project, 201);
      if (url.endsWith("/api/projects")) return response([]);
      if (url.endsWith("/api/clips")) return response([]);
      if (url.endsWith("/api/jobs")) return response([]);
      return response({ detail: "Not found" }, 404);
    }));
  });

  it("navigates between all sidebar views and marks the active view", async () => {
    render(<App />);
    await screen.findByText("No projects yet");
    for (const [label, heading] of [["My Clips", "No clips yet"], ["Projects", "Projects"], ["Processing Queue", "Queue is empty"], ["Settings", "Gemini API configuration"], ["About", "AIClipping Desktop"]]) {
      const navigation = within(screen.getByRole("navigation"));
      fireEvent.click(navigation.getByRole("button", { name: label }));
      expect(await screen.findByText(heading, { selector: "h2" })).toBeTruthy();
      expect(navigation.getByRole("button", { name: label }).getAttribute("aria-current")).toBe("page");
    }
  });

  it("opens the same project form from both New Project buttons and creates a project", async () => {
    render(<App />);
    await screen.findByText("No projects yet");
    fireEvent.click(within(document.querySelector("header.page-header") as HTMLElement).getByRole("button", { name: /New Project/ }));
    expect(screen.getByLabelText(/Project name/)).toBeTruthy();
    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "New Project" }));
    fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: "Test project" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    await screen.findByText(/Project created: Test project/);
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/api/projects"), expect.objectContaining({ method: "POST" })));
  });

  it("sends only one create request when the form is submitted repeatedly", async () => {
    let releaseCreate: ((value: Response) => void) | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/projects") && init?.method === "POST") return new Promise<Response>(resolve => { releaseCreate = resolve; });
      if (url.endsWith("/api/health")) return Promise.resolve(response({ status: "ok", database_ready: true, mode: "local" }));
      if (url.endsWith("/api/projects")) return Promise.resolve(response([]));
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return Promise.resolve(response([]));
      return Promise.resolve(response({ detail: "Not found" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    await screen.findByText("No projects yet");
    fireEvent.click(within(document.querySelector("header.page-header") as HTMLElement).getByRole("button", { name: /New Project/ }));
    fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: "Single submission" } });
    const form = screen.getByLabelText(/Project name/).closest("form");
    fireEvent.submit(form!);
    fireEvent.submit(form!);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    releaseCreate?.(response({ ...project, id: "single", name: "Single submission" }, 201));
    await screen.findByText("Project created: Single submission");
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    expect(await screen.findAllByRole("button", { name: "Open Single submission" })).toHaveLength(1);
  });

  it("uploads a selected video, reports preparation metadata, and shows upload errors", async () => {
    const source = { id: "s1", source_type: "local_file", source_uri: "clip.mp4", title: "clip.mp4", created_at: "2026-01-01", local_path: "C:/data/clip.mp4", duration_seconds: 1, metadata_json: "{}" };
    const preparation = { duration_seconds: 1, width: 64, height: 48, frame_rate: 25, video_codec: "mpeg4", audio_available: false, audio_codecs: [], format_name: "mov,mp4", readable: true, preparation_message: "Video stream readable; no audio stream found." };
    mockUpload(201, { source_video: source, preparation, size_bytes: 12 });
    render(<App />);
    await screen.findByText("No projects yet");
    fireEvent.click(within(document.querySelector("header.page-header") as HTMLElement).getByRole("button", { name: /New Project/ }));
    fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: "Test project" } });
    fireEvent.change(screen.getByLabelText(/Local video file/), { target: { files: [new File(["video"], "clip.mp4", { type: "video/mp4" })] } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("Video prepared")).toBeTruthy();
    expect(screen.getByText(/64×48/)).toBeTruthy();
    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "Projects" }));
    expect(await screen.findAllByRole("button", { name: "Open Test project" })).toHaveLength(1);

    mockUpload(422, { detail: "FFmpeg could not decode this video." });
    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "New Project" }));
    fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: "Bad upload" } });
    fireEvent.change(screen.getByLabelText(/Local video file/), { target: { files: [new File(["bad"], "bad.mp4", { type: "video/mp4" })] } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect((await screen.findByRole("alert")).textContent).toContain("FFmpeg could not decode this video.");
  });

  it("requires delete confirmation and removes the project after confirmed deletion", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects") && init?.method === "POST") return response(project, 201);
      if (url.endsWith("/api/projects/p1") && init?.method === "DELETE") return response({ deleted: true, cleanup_pending: false });
      if (url.endsWith("/api/projects")) return response([]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      return response({ detail: "Not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirmMock = vi.fn().mockReturnValue(false);
    vi.stubGlobal("confirm", confirmMock);
    render(<App />);
    await screen.findByText("No projects yet");
    fireEvent.click(within(document.querySelector("header.page-header") as HTMLElement).getByRole("button", { name: /New Project/ }));
    fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: project.name } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    await screen.findByText(`Project created: ${project.name}`);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    expect(await screen.findByRole("button", { name: "Open Test project" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Delete Test project" }));
    expect(confirmMock).toHaveBeenCalledWith(expect.stringContaining("locally stored video"));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
    expect(screen.getByRole("button", { name: "Open Test project" })).toBeTruthy();

    confirmMock.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Delete Test project" }));
    expect(await screen.findByText("No projects yet")).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  });

  it("shows actionable project deletion errors and leaves the project visible", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects/p1") && init?.method === "DELETE") return response({ detail: "This project has an active processing job." }, 409);
      if (url.endsWith("/api/projects")) return response([project]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      return response({ detail: "Not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn().mockReturnValue(true));
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete Test project" }));
    expect((await screen.findByRole("alert")).textContent).toContain("active processing job");
    expect(screen.getByRole("button", { name: "Open Test project" })).toBeTruthy();
  });

  it("starts transcription, shows stages and transcript, and exposes downloads", async () => {
    const transcript = { metadata: { project_id: "p1", source_filename: "source.mp4", language: "en", language_probability: 0.99, model: "small", device: "cpu", processed_at: "2026-01-01T00:00:00Z", duration_seconds: 1 }, segments: [{ start: 0.2, end: 0.8, text: "Hello there.", words: [] }], words: [{ start: 0.2, end: 0.4, text: " Hello" }] };
    let releaseStart: ((value: Response) => void) | undefined;
    let statusCalls = 0;
    vi.stubGlobal("fetch", vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return Promise.resolve(response({ status: "ok", database_ready: true, mode: "local" }));
      if (url.endsWith("/api/projects") && init?.method === "POST") return Promise.resolve(response(project, 201));
      if (url.endsWith("/api/projects")) return Promise.resolve(response([project]));
      if (url.endsWith("/api/clips")) return Promise.resolve(response([]));
      if (url.endsWith("/api/jobs")) return Promise.resolve(response([]));
      if (url.endsWith("/api/projects/p1/transcript")) return Promise.resolve(response(transcript));
      if (url.endsWith("/api/projects/p1/transcription") && init?.method === "POST") return new Promise<Response>(resolve => { releaseStart = resolve; });
      if (url.endsWith("/api/projects/p1/transcription")) return Promise.resolve(response({ detail: "No previous job" }, 404));
      if (url.includes("/api/projects/p1/transcription/jobs/")) {
        statusCalls += 1;
        const status = statusCalls === 1 ? "processing" : "completed";
        return Promise.resolve(response({ id: "job1", project_id: "p1", status, stage: status === "completed" ? "completed" : "transcribing", error_message: null, model_name: "small", device: "cpu", language: "en", started_at: null, completed_at: null, updated_at: "2026-01-01T00:00:00Z" }));
      }
      return Promise.resolve(response({ detail: "Not found" }, 404));
    }));

    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Test project" }));
    fireEvent.click(screen.getByRole("button", { name: "Process Video" }));
    expect(screen.getByRole("button", { name: "Starting…" }).hasAttribute("disabled")).toBe(true);
    releaseStart?.(response({ id: "job1", project_id: "p1", status: "queued", stage: "queued", error_message: null, model_name: "small", device: "cuda", language: null, started_at: null, completed_at: null, updated_at: "2026-01-01T00:00:00Z" }, 202));
    expect(await screen.findByText("Transcribing audio", {}, { timeout: 4000 })).toBeTruthy();
    expect(await screen.findByText("Transcription complete", {}, { timeout: 4000 })).toBeTruthy();
    expect(await screen.findByText("Hello there.", {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByRole("link", { name: "Download TXT" }).getAttribute("href")).toContain("/transcript/txt");
    expect(screen.getByRole("link", { name: "Download SRT" }).getAttribute("href")).toContain("/transcript/srt");
    expect(screen.getByRole("link", { name: "Download words JSON" }).getAttribute("href")).toContain("/transcript/json");
  });

  it("shows a useful transcription start error", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects")) return response([project]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      if (url.endsWith("/api/projects/p1/transcription") && init?.method === "POST") return response({ detail: "Import and prepare a local video before starting transcription." }, 422);
      if (url.endsWith("/api/projects/p1/transcription")) return response({ detail: "No previous job" }, 404);
      if (url.endsWith("/api/projects/p1/transcript")) return response({ detail: "No transcript" }, 404);
      return response({ detail: "Not found" }, 404);
    }));
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Test project" }));
    fireEvent.click(screen.getByRole("button", { name: "Process Video" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Import and prepare a local video");
  });
});
