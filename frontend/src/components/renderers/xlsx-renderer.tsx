"use client";

import { useState, useEffect } from "react";

const MAX_RENDERED_ROWS = 5_000;

interface XlsxRendererProps {
  data: ArrayBuffer;
  fileName: string;
}

interface ParsedWorkbook {
  sheetNames: string[];
  sheets: Record<string, string[][]>;
}

export function XlsxRenderer({ data, fileName }: XlsxRendererProps) {
  const [workbook, setWorkbook] = useState<ParsedWorkbook | null | "error">(
    null,
  );
  const [activeSheet, setActiveSheet] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setActiveSheet(0);
    import("xlsx")
      .then((XLSX) => {
        if (cancelled) return;
        try {
          const wb = XLSX.read(data, { type: "array" });
          const sheets: Record<string, string[][]> = {};
          for (const name of wb.SheetNames) {
            sheets[name] = XLSX.utils.sheet_to_json<string[]>(wb.Sheets[name], {
              header: 1,
            });
          }
          setWorkbook({ sheetNames: wb.SheetNames, sheets });
        } catch {
          setWorkbook("error");
        }
      })
      .catch(() => {
        if (!cancelled) setWorkbook("error");
      });
    return () => {
      cancelled = true;
    };
  }, [data]);

  if (workbook === null) {
    return (
      <div className="flex items-center justify-center gap-2 p-8 text-muted-foreground">
        <div className="h-5 w-5 animate-spin rounded-full border-2 border-current border-t-transparent" />
        <span>Loading spreadsheet...</span>
      </div>
    );
  }

  if (workbook === "error") {
    return (
      <div className="p-4 text-destructive">
        Failed to parse spreadsheet: {fileName}
      </div>
    );
  }

  const { sheetNames, sheets } = workbook;
  const jsonData = sheets[sheetNames[activeSheet]] ?? [];

  if (jsonData.length === 0) {
    return <div className="p-4 text-muted-foreground">Empty spreadsheet</div>;
  }

  const headers = jsonData[0] || [];
  const allRows = jsonData.slice(1);
  const totalRows = allRows.length;
  const truncated = totalRows > MAX_RENDERED_ROWS;
  const rows = truncated ? allRows.slice(0, MAX_RENDERED_ROWS) : allRows;

  return (
    <div className="flex flex-col gap-2">
      {sheetNames.length > 1 && (
        <div className="flex flex-wrap gap-1 px-4 pt-3">
          {sheetNames.map((name, i) => (
            <button
              key={name}
              onClick={() => setActiveSheet(i)}
              className={`rounded-md border px-3 py-1 text-xs transition-colors ${
                i === activeSheet
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-muted text-muted-foreground hover:bg-accent"
              }`}
            >
              {name}
            </button>
          ))}
        </div>
      )}
      <div className="max-h-[600px] overflow-auto">
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 z-10">
            <tr className="bg-muted/80 backdrop-blur-xs">
              <th className="w-10 border-b border-r border-border px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                #
              </th>
              {headers.map((header, i) => (
                <th
                  key={i}
                  className="whitespace-nowrap border-b border-r border-border px-3 py-2 text-left text-xs font-medium text-foreground"
                >
                  {String(header ?? "")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIdx) => (
              <tr key={rowIdx} className="transition-colors hover:bg-muted/50">
                <td className="border-b border-r border-border px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
                  {rowIdx + 1}
                </td>
                {headers.map((_, colIdx) => (
                  <td
                    key={colIdx}
                    className="max-w-[300px] truncate whitespace-nowrap border-b border-r border-border px-3 py-1.5 text-xs text-foreground"
                  >
                    {String(row[colIdx] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="px-4 pb-3 text-xs text-muted-foreground">
        {truncated
          ? `Showing ${MAX_RENDERED_ROWS.toLocaleString()} of ${totalRows.toLocaleString()} rows × ${headers.length} columns`
          : `${totalRows.toLocaleString()} rows × ${headers.length} columns`}
        {sheetNames.length > 1 && ` · Sheet: ${sheetNames[activeSheet]}`}
      </div>
    </div>
  );
}
