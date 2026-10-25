"""Qdrant collection of published culture elements; SQLite stays the source of truth."""
import uuid

from qdrant_client import models


class CultureIndex:
    def __init__(self, client, embed, workspace, dim):
        self.client, self.embed, self.dim = client, embed, dim
        self.name = 'atlas_culture_' + workspace
        if self.client.collection_exists(self.name):
            config = self.client.get_collection(self.name).config.params.vectors
            if not isinstance(config, models.VectorParams) or config.size != dim or config.distance != models.Distance.COSINE:
                raise ValueError('文化素材集合必须使用单个 dense vector、相同维度及 Cosine 度量。')
        else:
            self._create()

    def _create(self):
        self.client.create_collection(self.name, vectors_config=models.VectorParams(size=self.dim, distance=models.Distance.COSINE))

    def reset(self):
        self.client.delete_collection(self.name)
        self._create()

    def count(self):
        return self.client.count(self.name, exact=True).count

    @staticmethod
    def point(element_id):
        return str(uuid.UUID(element_id))

    async def upsert(self, items):
        """items: [(element_id, text, payload)]"""
        vectors = await self.embed([text for _, text, _ in items], context='document')
        self.client.upsert(self.name, points=[
            models.PointStruct(id=self.point(eid), vector=vectors[i].tolist(), payload={**payload, 'element_id': eid})
            for i, (eid, _, payload) in enumerate(items)])

    def delete(self, element_ids):
        self.client.delete(self.name, points_selector=models.PointIdsList(points=[self.point(i) for i in element_ids]))

    async def search(self, query, *, limit=8, kinds=None, themes=None, threshold=0.2):
        vector = (await self.embed([query], context='query'))[0].tolist()
        must = [models.FieldCondition(key='type', match=models.MatchAny(any=list(kinds)))] if kinds else []
        if themes:
            must.append(models.FieldCondition(key='themes', match=models.MatchAny(any=list(themes))))
        hits = self.client.query_points(self.name, query=vector, query_filter=models.Filter(must=must) if must else None,
                                        limit=limit, score_threshold=threshold, with_payload=True).points
        return [(h.payload['element_id'], h.score) for h in hits]
