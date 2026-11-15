import { describe, expect, it, vi } from "vitest";
import {
  streamFlatGeobufFeatureCollection,
  validateBboxTuple,
} from "@/lib/flatgeobufClient";

async function* fakeFeatureIterator() {
  yield {
    type: "Feature" as const,
    id: "p-1",
    geometry: {
      type: "MultiPolygon",
      coordinates: [[[[77.2, 28.6], [77.21, 28.6], [77.21, 28.61], [77.2, 28.61], [77.2, 28.6]]]],
    },
    properties: { parcel_identifier: "P-101", is_disputed: false },
  };
  yield {
    type: "Feature" as const,
    id: "p-2",
    geometry: {
      type: "MultiPolygon",
      coordinates: [[[[77.22, 28.6], [77.23, 28.6], [77.23, 28.61], [77.22, 28.61], [77.22, 28.6]]]],
    },
    properties: { parcel_identifier: "P-102", is_disputed: true },
  };
}

describe("FlatGeobuf streaming client", () => {
  it("validates WGS 84 bounding box tuples", () => {
    expect(validateBboxTuple([77.1, 28.5, 77.3, 28.7])).toEqual({
      minX: 77.1,
      minY: 28.5,
      maxX: 77.3,
      maxY: 28.7,
    });

    expect(() => validateBboxTuple([77.5, 28.5, 77.1, 28.7])).toThrow(/Invalid bbox/);
    expect(() => validateBboxTuple([77.1, 28.9, 77.3, 28.5])).toThrow(/Invalid bbox/);
  });

  it("streams FlatGeobuf features into a GeoJSON FeatureCollection and reports progress", async () => {
    const progressCalls: number[] = [];
    const mockDeserialize = vi.fn().mockReturnValue(fakeFeatureIterator());

    const collection = await streamFlatGeobufFeatureCollection("layer-123", {
      bbox: [77.1, 28.5, 77.3, 28.7],
      onProgress: (count) => progressCalls.push(count),
      deserializer: mockDeserialize,
    });

    expect(mockDeserialize).toHaveBeenCalledWith(
      "/api/v1/layers/layer-123/flatgeobuf?bbox=77.1,28.5,77.3,28.7",
      { minX: 77.1, minY: 28.5, maxX: 77.3, maxY: 28.7 }
    );
    expect(collection.type).toBe("FeatureCollection");
    expect(collection.features).toHaveLength(2);
    expect(collection.features[0].properties.parcel_identifier).toBe("P-101");
    expect(progressCalls).toEqual([1, 2]);
  });

  it("stops streaming when AbortSignal is aborted", async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(
      streamFlatGeobufFeatureCollection("layer-123", {
        signal: controller.signal,
        deserializer: () => fakeFeatureIterator(),
      })
    ).rejects.toThrow(/aborted/i);
  });
});
