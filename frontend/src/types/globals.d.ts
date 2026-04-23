// Ambient declarations for side-effect imports.
//
// TypeScript 6 made side-effect-only imports (``import "./foo.css"``)
// require a module declaration; earlier versions let them through without
// one.  Next.js still expects global CSS to be imported this way from the
// root app layout, so we ship a minimal shim.
declare module "*.css";
