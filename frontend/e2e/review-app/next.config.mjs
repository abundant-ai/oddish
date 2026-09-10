import path from "node:path";
export default {
  webpack(config) {
    config.resolve.alias["@clerk/nextjs"] = path.resolve("clerk.ts");
    return config;
  },
};
