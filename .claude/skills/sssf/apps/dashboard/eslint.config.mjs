import nextConfig from "eslint-config-next";

const config = [
  ...nextConfig,
  {
    ignores: [
      ".next/**",
      "next-env.d.ts",
    ],
  },
];

export default config;
