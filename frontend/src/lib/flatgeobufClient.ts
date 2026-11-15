import { geojson } from "flatgeobuf";
import { getFlatGeobufUrl } from "@/lib/api";
import type { GeoJSONGeometry } from "@/types/gis";

export interface FlatGeobufRect {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface ParcelGeoJSONFeature {
  type: "Feature";
  id?: string | number;
  geometry: GeoJSONGeometry | null;
  properties: Record<string, unknown>;
}

export interface ParcelGeoJSONFeatureCollection {
  type: "FeatureCollection";
  features: ParcelGeoJSONFeature[];
}

export type FlatGeobufDeserializer = (
  url: string,
  rect?: FlatGeobufRect
) => AsyncIterable<ParcelGeoJSONFeature>;

export interface StreamFlatGeobufOptions {
  bbox?: [number, number, number, number];
  signal?: AbortSignal;
  onProgress?: (loadedCount: number) => void;
  deserializer?: FlatGeobufDeserializer;
}

export function validateBboxTuple(
  bbox: [number, number, number, number]
): FlatGeobufRect {
  const [minX, minY, maxX, maxY] = bbox;
  if (
    !Number.isFinite(minX) ||
    !Number.isFinite(minY) ||
    !Number.isFinite(maxX) ||
    !Number.isFinite(maxY) ||
    minX >= maxX ||
    minY >= maxY ||
    minX < -180 ||
    maxX > 180 ||
    minY < -90 ||
    maxY > 90
  ) {
    throw new Error(
      `Invalid bbox [${bbox.join(", ")}]: expected [minLon, minLat, maxLon, maxLat] within WGS 84 bounds.`
    );
  }
  return { minX, minY, maxX, maxY };
}

const defaultDeserializer: FlatGeobufDeserializer = (url, rect) =>
  geojson.deserialize(url, rect) as AsyncIterable<ParcelGeoJSONFeature>;

export async function streamFlatGeobufFeatureCollection(
  layerId: string,
  options: StreamFlatGeobufOptions = {}
): Promise<ParcelGeoJSONFeatureCollection> {
  const { bbox, signal, onProgress, deserializer = defaultDeserializer } = options;

  if (signal?.aborted) {
    throw new Error("FlatGeobuf stream aborted before start");
  }

  const rect = bbox ? validateBboxTuple(bbox) : undefined;
  const url = getFlatGeobufUrl(layerId, bbox);
  const features: ParcelGeoJSONFeature[] = [];

  for await (const feature of deserializer(url, rect)) {
    if (signal?.aborted) {
      throw new Error("FlatGeobuf stream aborted");
    }
    features.push(feature);
    onProgress?.(features.length);
  }

  return {
    type: "FeatureCollection",
    features,
  };
}
