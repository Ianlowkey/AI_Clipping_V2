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

    mockUpload(422, { detail: "FFmpeg could not decode this video." });
    fireEvent.click(within(screen.getByRole("navigation", { name: "Primary navigation" })).getByRole("button", { name: "New Project" }));
    fireEvent.change(screen.getByLabelText(/Project name/), { target: { value: "Bad upload" } });
    fireEvent.change(screen.getByLabelText(/Local video file/), { target: { files: [new File(["bad"], "bad.mp4", { type: "video/mp4" })] } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect((await screen.findByRole("alert")).textContent).toContain("FFmpeg could not decode this video.");
  });
});
