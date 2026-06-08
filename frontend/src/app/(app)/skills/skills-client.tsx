"use client";

import { useCallback, useEffect, useState } from "react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// ── Types ────────────────────────────────────────────────────────────────────

type SkillFile = { relative_path: string; content: string };

type Skill = {
  id: string;
  org_id: string | null;
  created_by_user_id: string | null;
  name: string;
  description: string;
  is_seed: boolean;
  files: SkillFile[];
  created_at: string;
  updated_at: string;
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function buildSkillMd(
  name: string,
  description: string,
  body: string,
): string {
  return `---\nname: ${name}\ndescription: ${description}\n---\n\n${body}`;
}

/**
 * Split a SKILL.md content string into the markdown body (everything after
 * the closing `---` of the frontmatter).  We find the second `---` delimiter
 * and return everything that follows it, trimmed.
 */
function extractSkillMdBody(content: string): string {
  const parts = content.split("---");
  // parts[0] = "" (before opening ---), parts[1] = frontmatter, parts[2+] = body
  if (parts.length < 3) return content;
  return parts.slice(2).join("---").trimStart();
}

// ── Sub-component: extra-file row ─────────────────────────────────────────

interface ExtraFileRowProps {
  index: number;
  file: SkillFile;
  onChange: (index: number, updated: SkillFile) => void;
  onRemove: (index: number) => void;
}

function ExtraFileRow({ index, file, onChange, onRemove }: ExtraFileRowProps) {
  return (
    <div className="rounded-md border border-[#6f88b4]/20 bg-muted/20 p-3 space-y-2">
      <div className="flex items-center gap-2">
        <Input
          value={file.relative_path}
          onChange={(e) =>
            onChange(index, { ...file, relative_path: e.target.value })
          }
          placeholder="path/to/file.md"
          className="h-7 font-mono text-xs border-[#6f88b4]/20 flex-1"
        />
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 w-7 p-0 text-muted-foreground hover:text-destructive"
          onClick={() => onRemove(index)}
          aria-label="Remove file"
        >
          ×
        </Button>
      </div>
      <textarea
        value={file.content}
        onChange={(e) =>
          onChange(index, { ...file, content: e.target.value })
        }
        rows={4}
        placeholder="File content…"
        className="w-full rounded border border-[#6f88b4]/20 bg-background px-2 py-1.5 font-mono text-xs resize-y"
      />
    </div>
  );
}

// ── Sub-component: create/edit form panel ────────────────────────────────────

interface SkillFormProps {
  editingSkill: Skill | null;
  onSaved: () => void;
  onCancel: () => void;
}

function SkillForm({ editingSkill, onSaved, onCancel }: SkillFormProps) {
  const [name, setName] = useState(editingSkill?.name ?? "");
  const [description, setDescription] = useState(
    editingSkill?.description ?? "",
  );
  const [skillMdBody, setSkillMdBody] = useState<string>(() => {
    if (!editingSkill) return "";
    const skillMdFile = editingSkill.files.find(
      (f) => f.relative_path === "SKILL.md",
    );
    return skillMdFile ? extractSkillMdBody(skillMdFile.content) : "";
  });
  const [extraFiles, setExtraFiles] = useState<SkillFile[]>(() => {
    if (!editingSkill) return [];
    return editingSkill.files.filter((f) => f.relative_path !== "SKILL.md");
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isEdit = editingSkill !== null;

  function handleExtraFileChange(index: number, updated: SkillFile) {
    setExtraFiles((prev) => prev.map((f, i) => (i === index ? updated : f)));
  }

  function handleExtraFileRemove(index: number) {
    setExtraFiles((prev) => prev.filter((_, i) => i !== index));
  }

  function handleAddFile() {
    setExtraFiles((prev) => [...prev, { relative_path: "", content: "" }]);
  }

  async function handleSave() {
    if (!name.trim() || !description.trim()) {
      setError("Name and description are required.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const skillMdContent = buildSkillMd(name.trim(), description.trim(), skillMdBody);
      const files: SkillFile[] = [
        { relative_path: "SKILL.md", content: skillMdContent },
        ...extraFiles.filter((f) => f.relative_path.trim() !== ""),
      ];
      const payload = {
        name: name.trim(),
        description: description.trim(),
        files,
      };

      const url = isEdit
        ? `/api/skills/${editingSkill.id}`
        : "/api/skills";
      const method = isEdit ? "PUT" : "POST";

      const res = await fetch(url, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        let detail = isEdit ? "Failed to update skill" : "Failed to create skill";
        try {
          const data = (await res.json()) as { detail?: string };
          detail = data.detail ?? detail;
        } catch {
          // ignore parse error, keep default message
        }
        setError(detail);
        return;
      }

      onSaved();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card className="border-[#6f88b4]/20 shadow-xs">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">
          {isEdit ? `Edit skill: ${editingSkill.name}` : "New skill"}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="skill-name" className="text-xs font-medium">
              Name
            </Label>
            <Input
              id="skill-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-skill"
              maxLength={80}
              className="h-8 border-[#6f88b4]/20"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="skill-description" className="text-xs font-medium">
              Description
            </Label>
            <Input
              id="skill-description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this skill does"
              className="h-8 border-[#6f88b4]/20"
            />
          </div>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="skill-body" className="text-xs font-medium">
            SKILL.md body{" "}
            <span className="text-muted-foreground">(after frontmatter)</span>
          </Label>
          <textarea
            id="skill-body"
            value={skillMdBody}
            onChange={(e) => setSkillMdBody(e.target.value)}
            rows={10}
            placeholder="Describe what this skill does, how to trigger it, any constraints…"
            className="w-full rounded-md border border-[#6f88b4]/20 bg-background px-3 py-2 font-mono text-sm resize-y focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>

        {/* Extra files */}
        {extraFiles.length > 0 && (
          <div className="space-y-2">
            <p className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              Additional files
            </p>
            {extraFiles.map((file, index) => (
              <ExtraFileRow
                key={index}
                index={index}
                file={file}
                onChange={handleExtraFileChange}
                onRemove={handleExtraFileRemove}
              />
            ))}
          </div>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="h-7 text-xs border-[#6f88b4]/20"
          onClick={handleAddFile}
        >
          + Add file
        </Button>

        {error && (
          <Alert variant="destructive">
            <AlertTitle>Error</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="flex items-center gap-2 pt-1">
          <Button
            type="button"
            size="sm"
            onClick={() => void handleSave()}
            disabled={saving || !name.trim() || !description.trim()}
            className="h-8"
          >
            {saving ? "Saving…" : isEdit ? "Update skill" : "Create skill"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-8 border-[#6f88b4]/20"
            onClick={onCancel}
            disabled={saving}
          >
            Cancel
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Main client component ─────────────────────────────────────────────────────

export function SkillsClient() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // null = list view; "new" = create form; Skill = edit form
  const [formState, setFormState] = useState<"new" | Skill | null>(null);

  const loadSkills = useCallback(async () => {
    setLoading(true);
    setFetchError(null);
    try {
      const res = await fetch("/api/skills");
      if (!res.ok) {
        setFetchError(`Failed to load skills (HTTP ${res.status})`);
        return;
      }
      const data = (await res.json()) as Skill[];
      setSkills(data);
    } catch (err: unknown) {
      setFetchError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadSkills();
  }, [loadSkills]);

  async function handleDelete(skill: Skill) {
    if (!confirm(`Delete skill "${skill.name}"? This cannot be undone.`)) {
      return;
    }
    try {
      const res = await fetch(`/api/skills/${skill.id}`, { method: "DELETE" });
      if (!res.ok) {
        alert(`Failed to delete skill (HTTP ${res.status})`);
        return;
      }
      await loadSkills();
    } catch (err: unknown) {
      alert(err instanceof Error ? err.message : String(err));
    }
  }

  function handleFormSaved() {
    setFormState(null);
    void loadSkills();
  }

  function handleFormCancel() {
    setFormState(null);
  }

  // ── Render: form panel ──────────────────────────────────────────────────────

  if (formState !== null) {
    return (
      <div className="space-y-4">
        <SkillForm
          editingSkill={formState === "new" ? null : formState}
          onSaved={handleFormSaved}
          onCancel={handleFormCancel}
        />
      </div>
    );
  }

  // ── Render: list ────────────────────────────────────────────────────────────

  return (
    <div className="space-y-6">
      <Card className="border-[#6f88b4]/20 shadow-xs">
        <CardHeader className="flex flex-col gap-3 pb-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="space-y-1">
            <CardTitle className="text-base">Skills library</CardTitle>
            <p className="text-[11px] text-muted-foreground">
              Shared skills available to probe agents in this org.
            </p>
          </div>
          <Button
            type="button"
            size="sm"
            className="h-8 text-xs"
            onClick={() => setFormState("new")}
          >
            + New skill
          </Button>
        </CardHeader>
        <CardContent>
          {fetchError ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load skills</AlertTitle>
              <AlertDescription>{fetchError}</AlertDescription>
            </Alert>
          ) : loading ? (
            <div className="py-8 text-center text-sm text-muted-foreground">
              Loading…
            </div>
          ) : skills.length === 0 ? (
            <div className="rounded-lg border border-dashed border-[#6f88b4]/30 bg-card/60 px-6 py-10 text-center text-sm text-muted-foreground">
              No skills yet. Create one to get started.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-[180px]">Name</TableHead>
                  <TableHead>Description</TableHead>
                  <TableHead className="w-[160px]">Uploaded by</TableHead>
                  <TableHead className="w-[90px]">Type</TableHead>
                  <TableHead className="w-[120px] text-right">
                    Actions
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {skills.map((skill) => (
                  <TableRow key={skill.id}>
                    <TableCell className="font-mono text-xs font-medium">
                      {skill.name}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {skill.description || (
                        <span className="italic">No description</span>
                      )}
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {skill.is_seed
                        ? "built-in"
                        : (skill.created_by_user_id ?? "—")}
                    </TableCell>
                    <TableCell>
                      {skill.is_seed ? (
                        <Badge
                          variant="secondary"
                          className="text-[10px] px-1.5 py-0"
                        >
                          Seed
                        </Badge>
                      ) : (
                        <Badge
                          variant="outline"
                          className="text-[10px] px-1.5 py-0 border-[#6f88b4]/30"
                        >
                          Custom
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {!skill.is_seed && (
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-6 px-2 text-[11px]"
                            onClick={() => setFormState(skill)}
                          >
                            Edit
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="h-6 px-2 text-[11px] text-red-500 hover:text-red-600 hover:bg-red-500/10"
                            onClick={() => void handleDelete(skill)}
                          >
                            Delete
                          </Button>
                        </div>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
