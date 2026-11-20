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
});
