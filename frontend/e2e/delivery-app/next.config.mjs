import { fileURLToPath } from "node:url";
export default {
  webpack(config) {
    config.resolve.alias["@clerk/nextjs"] = fileURLToPath(
      new URL("./clerk.ts", import.meta.url)
    );
    return config;
  },
};
