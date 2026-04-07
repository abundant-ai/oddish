import { Button } from "@/components/ui/button";

interface AgentLegendItem {
  key: string;
  label: string;
  color: string;
}

interface AgentLegendProps {
  items: AgentLegendItem[];
  hiddenKeys: Set<string>;
  onToggle: (key: string) => void;
}

export function AgentLegend({ items, hiddenKeys, onToggle }: AgentLegendProps) {
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {items.map((item) => {
        const isHidden = hiddenKeys.has(item.key);
        return (
          <Button
            key={item.key}
            type="button"
            onClick={() => onToggle(item.key)}
            variant="ghost"
            size="sm"
            className={`flex h-auto items-center gap-2 rounded px-2 py-1 font-mono text-xs transition-all ${
              isHidden ? "opacity-40 hover:opacity-60" : "hover:bg-muted"
            }`}
            title={isHidden ? "Click to show" : "Click to hide"}
          >
            <span
              className="h-3 w-3 flex-shrink-0 rounded-sm"
              style={{
                backgroundColor: isHidden ? "transparent" : item.color,
                border: `2px solid ${item.color}`,
              }}
            />
            <span
              className={isHidden ? "text-muted-foreground line-through" : ""}
            >
              {item.label}
            </span>
          </Button>
        );
      })}
    </div>
  );
}
