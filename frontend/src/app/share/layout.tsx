import { Nav } from "@/components/nav";

export default function ShareLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <>
      <Nav />
      {children}
    </>
  );
}
