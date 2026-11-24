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

  it("saves Gemini keys without retaining or displaying them or starting analysis", async () => {
    const secret = "AIzaSy" + "X".repeat(35);
    const status = { configured: false, model: "gemini-3.6-flash", key_environment_variable: "AICLIPPING_GEMINI_API_KEY", saved_in_credential_manager: false, secure_storage_available: true, source: "none" };
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects")) return response([]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      if (url.endsWith("/api/settings/gemini") && init?.method === undefined) return response(status);
      if (url.endsWith("/api/settings/gemini/key") && init?.method === "PUT") return response({ ...status, configured: true, saved_in_credential_manager: true, source: "credential_manager" });
      if (url.endsWith("/api/settings/gemini/key") && init?.method === "DELETE") return response(status);
      return response({ detail: "Not found" }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Settings" }));
    const input = await screen.findByLabelText("Gemini API key") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
    fireEvent.change(input, { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: "Save API key" }));
    expect(await screen.findByText("Gemini API key saved securely. No analysis was started.")).toBeTruthy();
    await waitFor(() => expect(input.value).toBe(""));
    expect(screen.queryByText(secret)).toBeNull();
    expect(screen.getByRole("button", { name: "Replace saved key" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Delete saved key" }));
    expect(await screen.findByText("Saved Gemini API key deleted.")).toBeTruthy();
    expect(screen.getByText("API key: Not configured")).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/highlights"))).toBe(false);
    const saveRequest = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/api/settings/gemini/key") && init?.method === "PUT");
    expect(JSON.parse(String(saveRequest?.[1]?.body)).api_key).toBe(secret);
  });

  it("explains when secure Gemini key storage is unavailable and blocks saving", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects")) return response([]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      if (url.endsWith("/api/settings/gemini")) return response({ configured: false, model: "gemini-3.6-flash", key_environment_variable: "AICLIPPING_GEMINI_API_KEY", saved_in_credential_manager: false, secure_storage_available: false, source: "none" });
      return response({ detail: "Not found" }, 404);
    }));
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Settings" }));
    const input = await screen.findByLabelText("Gemini API key") as HTMLInputElement;
    expect((await screen.findByRole("alert")).textContent).toContain("Windows Credential Manager is unavailable");
    expect(input.disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Save API key" }) as HTMLButtonElement).disabled).toBe(true);
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
    const transcript = { metadata: { project_id: "p1", source_filename: "source.mp4", language: "en", language_probability: 0.99, model: "small", device: "cpu", compute_type: "int8", processed_at: "2026-01-01T00:00:00Z", duration_seconds: 1, diarization_status: "complete", diarization_message: "Estimated locally", speakers: [{ speaker_id: "speaker_1", label: "Speaker 1" }], speaker_turns: [{ start: 0.2, end: 0.8, speaker_id: "speaker_1" }] }, segments: [{ start: 0.2, end: 0.8, text: "Hello there.", speaker_id: "speaker_1", words: [{ start: 0.2, end: 0.8, text: " Hello there.", speaker_id: "speaker_1" }] }], words: [{ start: 0.2, end: 0.4, text: " Hello", speaker_id: "speaker_1" }] };
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
      if (url.endsWith("/api/projects/p1/transcript-folder")) return Promise.resolve(response({ path: "C:\\Users\\test\\AIClippingData\\projects\\p1\\transcript" }));
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
    expect(await screen.findByText("Transcribing", {}, { timeout: 4000 })).toBeTruthy();
    expect(await screen.findByText("Completed", {}, { timeout: 4000 })).toBeTruthy();
    expect(await screen.findByText("Speaker 1: Hello there.", {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.getByText(/Speaker diarization completed — local speaker-turn estimates/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "Download transcript.txt" }).getAttribute("href")).toContain("/transcript/txt");
    expect(screen.getByRole("link", { name: "Download transcript.srt" }).getAttribute("href")).toContain("/transcript/srt");
    expect(screen.getByRole("link", { name: "Download words.json" }).getAttribute("href")).toContain("/transcript/json");
    expect(screen.getByText(/Compute: int8/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open Transcript Folder" }));
    expect(await screen.findByText(/Browser mode cannot open local folders/)).toBeTruthy();
    expect(await screen.findByText(/C:\\Users\\test\\AIClippingData/)).toBeTruthy();
  });

  it("distinguishes unavailable diarization while preserving the transcript", async () => {
    const transcript = {
      metadata: {
        project_id: "p1", source_filename: "source.mp4", language: "en",
        language_probability: 0.99, model: "small", device: "cpu", compute_type: "int8",
        processed_at: "2026-01-01", duration_seconds: 2,
        diarization_status: "unavailable", diarization_detail: "dependency_not_installed",
        diarization_message: "Optional pyannote.audio support is not installed.",
        speakers: null, speaker_turns: null,
      },
      segments: [{ start: 0.2, end: 1.2, text: "Hello there.", speaker_id: null,
        words: [{ start: 0.2, end: 1.2, text: " Hello there.", speaker_id: null }] }],
      words: [{ start: 0.2, end: 1.2, text: " Hello there.", speaker_id: null }],
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects")) return response([project]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      if (url.endsWith("/api/projects/p1/transcript")) return response(transcript);
      if (url.endsWith("/api/projects/p1/transcription")) return response({ detail: "No job" }, 404);
      if (url.endsWith("/api/projects/p1/highlights/candidates")) return response([]);
      return response({ detail: "Not found" }, 404);
    }));
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Test project" }));
    expect(await screen.findByText(/Diarization unavailable — optional pyannote\/Torch dependencies are not installed/)).toBeTruthy();
    expect(screen.getByText("Hello there.", { exact: false })).toBeTruthy();
  });

  it("starts local rendering for approved selected candidates and shows exports", async () => {
    const candidate = {
      id:"c1", project_id:"p1", transcript_id:"t1", start_seconds:20, end_seconds:50,
      duration_seconds:30, excerpt:"A podcast exchange", title:"Podcast moment", hook:"The reaction",
      category:"story", selection_explanation:"Complete", score:80, hook_strength:80,
      engagement_potential:85, standalone_clarity:90, emotional_value:70, comedy_potential:null,
      comedy_explanation:null, conversational_engagement:90, storytelling_interest:85,
      surprise_potential:75, emotional_impact:80, clip_completeness:95, speaker_context:"Speaker 1: setup",
      review_status:"approved", selected_for_render:true, validation_warnings:null,
      created_at:"2026-01-01",
    };
    const exportItem = {
      id:"e1", project_id:"p1", candidate_id:"c1", file_path:"exports/candidate.mp4",
      filename:"candidate_c1_vertical_123.mp4", duration_seconds:30, output_profile:"vertical",
      file_size_bytes:1024*1024, width:1080, height:1920, created_at:"2026-01-01",
    };
    let renderProfile = "";
    let renderedCandidateIds: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status:"ok", database_ready:true, mode:"local" });
      if (url.endsWith("/api/projects")) return response([project]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      if (url.endsWith("/api/projects/p1/transcript")) return response({ detail:"No transcript" },404);
      if (url.endsWith("/api/projects/p1/transcription")) return response({ detail:"No job" },404);
      if (url.endsWith("/api/projects/p1/highlights/candidates")) return response([candidate]);
      if (url.endsWith("/api/projects/p1/highlights")) return response({ detail:"No analysis" },404);
      if (url.endsWith("/api/projects/p1/render") && init?.method==="POST") {
        const body = JSON.parse(String(init.body));
        renderProfile=body.output_profile;
        renderedCandidateIds=body.candidate_ids;
        return response({ id:"r1", project_id:"p1", status:"queued", stage:"queued", progress_percent:0,
          error_message:null, output_profile:renderProfile, candidate_count:1, started_at:null,
          completed_at:null, updated_at:"2026-01-01" },202);
      }
      if (url.endsWith("/api/projects/p1/render/jobs/r1")) return response({ id:"r1", project_id:"p1", status:"completed",
        stage:"completed", progress_percent:100, error_message:null, output_profile:"vertical",
        candidate_count:1, started_at:null, completed_at:"2026-01-01", updated_at:"2026-01-01" });
      if (url.endsWith("/api/projects/p1/exports")) return response([exportItem]);
      return response({ detail:"Not found" },404);
    }));
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button",{name:"Projects"}));
    fireEvent.click(await screen.findByRole("button",{name:"Open Test project"}));
    await screen.findByText("Podcast moment");
    fireEvent.change(screen.getByLabelText("Render output profile"),{target:{value:"square"}});
    fireEvent.click(screen.getByRole("button",{name:"Render selected clips"}));
    await waitFor(()=>expect(renderProfile).toBe("square"));
    expect(renderedCandidateIds).toEqual(["c1"]);
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
      if (url.endsWith("/api/projects/p1/highlights/candidates")) return response([]);
      return response({ detail: "Not found" }, 404);
    }));
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Test project" }));
    fireEvent.click(screen.getByRole("button", { name: "Process Video" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Import and prepare a local video");
  });

  it("runs highlight analysis and supports candidate editing and review", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const candidate = { id: "c1", project_id: "p1", transcript_id: "t1", start_seconds: 20, end_seconds: 50, duration_seconds: 30, excerpt: "A real transcript passage", title: "Strong moment", hook: "Why this matters", category: "insight", selection_explanation: "Self-contained", score: 82, hook_strength: 80, engagement_potential: 82, standalone_clarity: 90, emotional_value: 70, comedy_potential: 91, comedy_explanation: "The reply unexpectedly flips the earlier setup.", review_status: "suggested", selected_for_render: false, validation_warnings: null, created_at: "2026-01-01T00:00:00Z" };
    let candidates: typeof candidate[] = [];
    let poll = 0;
    let requestedMode = "";
    let requestedProfile = "";
    let renderedCandidateIds: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/health")) return response({ status: "ok", database_ready: true, mode: "local" });
      if (url.endsWith("/api/projects")) return response([project]);
      if (url.endsWith("/api/clips") || url.endsWith("/api/jobs")) return response([]);
      if (url.endsWith("/api/projects/p1/transcript")) return response({ detail: "No transcript" }, 404);
      if (url.endsWith("/api/projects/p1/transcription")) return response({ detail: "No job" }, 404);
      if (url.endsWith("/api/projects/p1/highlights/candidates")) return response(candidates);
      if (url.endsWith("/api/projects/p1/highlights") && init?.method === "POST") { const body = JSON.parse(String(init.body)); requestedMode = body.detection_mode; requestedProfile = body.content_profile; return response({ id: "h1", project_id: "p1", status: "queued", stage: "queued", error_message: null, model_name: "gemini-3.6-flash", detection_mode: body.detection_mode, content_profile: body.content_profile, started_at: null, completed_at: null, updated_at: "2026-01-01T00:00:00Z" }, 202); }
      if (url.endsWith("/api/projects/p1/highlights/jobs/h1")) { poll++; return response({ id: "h1", project_id: "p1", status: poll < 2 ? "processing" : "completed", stage: poll < 2 ? "calling_gemini" : "completed", error_message: null, model_name: "gemini-3.6-flash", detection_mode: "comedy", content_profile: "podcast_conversation", started_at: null, completed_at: null, updated_at: "2026-01-01T00:00:00Z" }); }
      if (url.endsWith("/candidates/c1") && init?.method === "PATCH") { const body = JSON.parse(String(init.body)); Object.assign(candidate, body, { review_status: "edited" }); return response(candidate); }
      if (url.endsWith("/candidates/c1/review") && init?.method === "PATCH") { const body = JSON.parse(String(init.body)); Object.assign(candidate, { review_status: body.status, selected_for_render: body.status === "rejected" ? false : candidate.selected_for_render }); return response(candidate); }
      if (url.endsWith("/candidates/c1/selection") && init?.method === "PATCH") { const body = JSON.parse(String(init.body)); if (candidate.review_status !== "approved" && body.selected) return response({ detail: "Only approved candidates can be selected" }, 409); Object.assign(candidate, { selected_for_render: body.selected }); return response(candidate); }
      if (url.endsWith("/api/projects/p1/render") && init?.method === "POST") { renderedCandidateIds = JSON.parse(String(init.body)).candidate_ids; return response({ id: "r1", project_id: "p1", status: "completed", stage: "completed", progress_percent: 100, error_message: null, output_profile: "vertical", candidate_count: renderedCandidateIds.length, started_at: null, completed_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }, 202); }
      return response({ detail: "Not found" }, 404);
    }));
    render(<App />);
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Test project" }));
    fireEvent.change(screen.getByLabelText("Detection mode"), { target: { value: "comedy" } });
    expect(screen.getByLabelText("Content profile")).toHaveProperty("value", "podcast_conversation");
    fireEvent.click(screen.getByRole("button", { name: "Find Highlights" }));
    expect(requestedMode).toBe("comedy");
    expect(requestedProfile).toBe("podcast_conversation");
    expect((window.confirm as ReturnType<typeof vi.fn>).mock.calls.at(-1)?.[0]).toContain("cannot analyse facial expressions");
    expect(await screen.findByText("Calling Gemini")).toBeTruthy();
    expect(await screen.findByText(/Detection mode: Funny moments \/ comedy/)).toBeTruthy();
    candidates = [candidate];
    expect(await screen.findByText("Strong moment")).toBeTruthy();
    expect(await screen.findByText("Comedy potential: 91/100")).toBeTruthy();
    expect(await screen.findByText("Why this may be funny: The reply unexpectedly flips the earlier setup.")).toBeTruthy();
    const preview = screen.getByLabelText("Preview Strong moment") as HTMLVideoElement;
    expect(preview.getAttribute("src")).toContain("/api/projects/p1/source-video/preview");
    Object.defineProperty(preview, "currentTime", { configurable: true, writable: true, value: 0 });
    fireEvent.loadedMetadata(preview);
    expect(preview.currentTime).toBe(20);
    preview.currentTime = 80;
    fireEvent.seeking(preview);
    expect(preview.currentTime).toBe(20);
    const pause = vi.fn();
    Object.defineProperty(preview, "pause", { configurable: true, value: pause });
    preview.currentTime = 50;
    fireEvent.timeUpdate(preview);
    expect(pause).toHaveBeenCalledOnce();
    expect(preview.currentTime).toBe(20);
    fireEvent.change(screen.getByLabelText("Title", { selector: "input" }), { target: { value: "Edited highlight" } });
    fireEvent.click(screen.getByRole("button", { name: "Save edits" }));
    await waitFor(() => expect(candidate.title).toBe("Edited highlight"));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(candidate.review_status).toBe("approved"));
    expect(await screen.findByText("Selected for rendering: 0")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Select for rendering"));
    await waitFor(() => expect(candidate.selected_for_render).toBe(true));
    expect(await screen.findByText("Selected for rendering: 1")).toBeTruthy();
    fireEvent.click(within(screen.getByRole("navigation")).getByRole("button", { name: "Projects" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open Test project" }));
    expect(await screen.findByText("Selected for rendering: 1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Render selected clips" }));
    await waitFor(() => expect(renderedCandidateIds).toEqual(["c1"]));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(candidate.review_status).toBe("rejected"));
    expect(candidate.selected_for_render).toBe(false);
    expect(await screen.findByText("Selected for rendering: 0")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Render selected clips" }));
    expect((await screen.findByRole("alert")).textContent).toContain("No candidates are selected");
  });
});
