import "../../../src/app/globals.css";
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <main style={{ padding: 24 }}>{children}</main>
      </body>
    </html>
  );
}
