// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AgentDashHUD",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "adash-hud", targets: ["adash-hud"]),
        .library(name: "HUDCore", targets: ["HUDCore"]),
    ],
    targets: [
        .target(
            name: "HUDCore"
        ),
        .executableTarget(
            name: "adash-hud",
            dependencies: ["HUDCore"]
        ),
        .testTarget(
            name: "HUDCoreTests",
            dependencies: ["HUDCore"],
            resources: [
                .copy("Fixtures/snapshot.json")
            ]
        ),
    ]
)
