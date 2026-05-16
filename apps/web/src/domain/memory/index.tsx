import {
  type Lesson,
  useCreateLesson,
  useDeleteLesson,
  useGithubRepositories,
  useLessons,
} from "@core/api";
import { Button, Card, CardContent, CardHeader } from "@shared/components";
import { useState } from "react";

export function MemoryPage() {
  const { data: lessons } = useLessons();
  const { data: repos } = useGithubRepositories();
  const create = useCreateLesson();
  const remove = useDeleteLesson();

  const [repoExternalId, setRepoExternalId] = useState<string>("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");

  // Build the picker options: union of repos the App can see + repos that
  // already have lessons (in case the App's access shrank but the lessons
  // remain). Sorted, deduped.
  const repoNamesFromLessons = new Set((lessons ?? []).map((l) => l.repo_external_id));
  const repoNamesFromInstall = new Set(
    (repos?.repositories ?? []).map((r) => r.full_name).filter(Boolean) as string[],
  );
  const pickerOptions = Array.from(
    new Set([...repoNamesFromInstall, ...repoNamesFromLessons]),
  ).sort();

  return (
    <div className="mx-auto max-w-[900px] flex flex-col gap-3">
      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Add a lesson</h2>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-col gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              if (!repoExternalId || !title.trim() || !body.trim()) return;
              create.mutate(
                { repo_external_id: repoExternalId, title: title.trim(), body: body.trim() },
                {
                  onSuccess: () => {
                    setTitle("");
                    setBody("");
                  },
                },
              );
            }}
          >
            <select
              data-testid="lesson-repo"
              value={repoExternalId}
              onChange={(e) => setRepoExternalId(e.target.value)}
              className="px-2 py-1.5 text-[12.5px] border border-border-soft rounded bg-bg"
            >
              <option value="">(select a repo)</option>
              {pickerOptions.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            {pickerOptions.length === 0 && (
              <p className="text-text-4 text-[11px]">
                No repos yet. Install the GitHub App on at least one repo to populate this list.
              </p>
            )}
            <input
              data-testid="lesson-title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="title"
              className="px-2 py-1.5 text-[12.5px] border border-border-soft rounded bg-bg"
            />
            <textarea
              data-testid="lesson-body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="body (≤1000 chars)"
              maxLength={1000}
              rows={3}
              className="px-2 py-1.5 text-[12.5px] border border-border-soft rounded bg-bg"
            />
            <Button type="submit" data-testid="lesson-save" disabled={create.isPending}>
              Save
            </Button>
            {create.isError && (
              <div className="text-danger text-[12px]" data-testid="lesson-error">
                {(create.error as Error).message}
              </div>
            )}
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="font-semibold text-[13.5px]">Lessons</h2>
        </CardHeader>
        <CardContent>
          <ul className="flex flex-col gap-2" data-testid="lessons-list">
            {lessons?.map((l: Lesson) => (
              <li key={l.id} className="border border-border-soft rounded p-2.5 text-[12.5px]">
                <div className="flex items-center gap-2">
                  <span className="font-medium flex-1">{l.title}</span>
                  <span className="mono text-text-4 text-[10.5px]">{l.repo_external_id}</span>
                  <button
                    type="button"
                    className="text-text-4 hover:text-danger text-[11px]"
                    onClick={() => remove.mutate(l.id)}
                  >
                    Delete
                  </button>
                </div>
                <p className="text-text-3 mt-1 whitespace-pre-wrap">{l.body}</p>
              </li>
            ))}
            {lessons && lessons.length === 0 && (
              <li className="text-text-3 text-[12.5px]">No lessons yet.</li>
            )}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
