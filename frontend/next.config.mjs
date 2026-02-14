/** @type {import('next').NextConfig} */
const nextConfig = {
  reactCompiler: true,
  // Allow dev server requests from buggy hostname (e.g. http://buggy:3000)
  allowedDevOrigins: ["buggy", "http://buggy:3000", "http://192.168.1.69:3000"],
};

export default nextConfig;
