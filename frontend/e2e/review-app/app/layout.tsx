import "./styles.css";
import { Providers } from "@/app/providers";
export default function Layout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <main className="p-6">
            <nav className="mb-6 flex gap-4">
              <a href="/deliveries/review-demo">Delivery fixture</a>
              <a href="/experiments/review-demo">Experiment fixture</a>
            </nav>
            {children}
          </main>
        </Providers>
      </body>
    </html>
  );
}
