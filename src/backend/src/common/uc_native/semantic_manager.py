"""Semantic models manager backed by UC Volumes + Delta triples."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from rdflib import OWL, RDF, RDFS, ConjunctiveGraph, Graph, Literal, URIRef
from rdflib.namespace import SKOS

from src.common.logging import get_logger
from src.common.uc_native.semantic import UcNativeSemanticStore
from src.models.ontology import OntologyConcept, SemanticModel as SemanticModelOntology
from src.models.semantic_models import SemanticModel as SemanticModelApi

logger = get_logger(__name__)

_TAXONOMY_CONTEXT = {
    "ontos-ontology.ttl": "urn:taxonomy:ontos-ontology",
    "databricks_ontology.ttl": "urn:taxonomy:databricks_ontology",
    "odcs-ontology.ttl": "urn:taxonomy:odcs-ontology",
}


def _local_name(iri: str) -> str:
    if "#" in iri:
        return iri.rsplit("#", 1)[-1]
    return iri.rsplit("/", 1)[-1] if "/" in iri else iri


def _extract_source_context(context_name: str) -> Optional[str]:
    for prefix in (
        "urn:taxonomy:",
        "urn:semantic-model:",
        "urn:schema:",
        "urn:glossary:",
        "urn:ontology:",
    ):
        if context_name.startswith(prefix):
            return context_name[len(prefix) :]
    return None


class UcNativeSemanticModelsManager:
    """UC-native semantic manager with an in-memory RDF graph for ontology features.

    Loads bundled taxonomies from ``data/taxonomies`` so OntologySchemaManager,
    Ontology Generator consumers, and Term Mapping concept lookup have a working
    ``_graph`` without Postgres ``rdf_triples``.
    """

    def __init__(
        self,
        semantic: UcNativeSemanticStore,
        *,
        data_dir: Optional[Path] = None,
    ) -> None:
        self._semantic = semantic
        self._graph = ConjunctiveGraph()
        self._data_dir = data_dir or Path(__file__).resolve().parents[2] / "data"
        taxonomy_dir = self._data_dir / "taxonomies"
        self._load_bundled_taxonomies(taxonomy_dir)

    def _load_bundled_taxonomies(self, taxonomy_dir: Path) -> None:
        if not taxonomy_dir.is_dir():
            logger.warning("UC-native taxonomy dir missing: %s", taxonomy_dir)
            return
        for path in sorted(taxonomy_dir.glob("*.ttl")):
            context_name = _TAXONOMY_CONTEXT.get(path.name, f"urn:taxonomy:{path.stem}")
            try:
                ctx = self._graph.get_context(URIRef(context_name))
                ctx.parse(path.as_posix(), format="turtle")
                logger.info(
                    "Loaded UC-native taxonomy %s into %s (%s triples)",
                    path.name,
                    context_name,
                    len(ctx),
                )
            except Exception as exc:
                logger.warning("Failed loading taxonomy %s: %s", path, exc, exc_info=True)

    # --- API surface expected by semantic_models_routes ---

    def list(self) -> List[SemanticModelApi]:
        """Customer/uploaded models (Postgres-backed in Lakebase). Empty in UC-native."""
        return []

    def get_taxonomies(self) -> List[SemanticModelOntology]:
        taxonomies: List[SemanticModelOntology] = []
        for context in self._graph.contexts():
            if not hasattr(context, "identifier"):
                continue
            context_id = context.identifier
            if not isinstance(context_id, URIRef):
                continue
            context_name = str(context_id)
            source = _extract_source_context(context_name)
            if not source:
                continue
            source_type = "file" if context_name.startswith("urn:taxonomy:") else "database"
            if context_name.startswith("urn:semantic-model:"):
                source_type = "database"
            taxonomies.append(
                SemanticModelOntology(
                    name=source,
                    display_name=source.replace("_", " ").replace("-", " ").title(),
                    description=None,
                    source_type=source_type,
                    format="ttl",
                    concepts_count=0,
                    properties_count=0,
                )
            )
        return taxonomies

    def bundled_taxonomy_file_size_bytes(self, name: str) -> Optional[int]:
        path = self._data_dir / "taxonomies" / f"{name}.ttl"
        if not path.is_file():
            # name may already include extension or use dashes
            for candidate in self._data_dir.joinpath("taxonomies").glob("*.ttl"):
                if candidate.stem == name or candidate.name == name:
                    path = candidate
                    break
        try:
            return path.stat().st_size if path.is_file() else None
        except OSError:
            return None

    def get_grouped_concepts(self) -> Dict[str, List[OntologyConcept]]:
        grouped: Dict[str, List[OntologyConcept]] = {}
        for context in self._graph.contexts():
            if not hasattr(context, "identifier"):
                continue
            context_id = context.identifier
            if not isinstance(context_id, URIRef):
                continue
            context_name = str(context_id)
            source = _extract_source_context(context_name) or "Unassigned"
            concepts: List[OntologyConcept] = []
            seen: set[str] = set()
            for cls_type in (OWL.Class, RDFS.Class, SKOS.Concept):
                for subj in context.subjects(RDF.type, cls_type):
                    if not isinstance(subj, URIRef):
                        continue
                    iri = str(subj)
                    if iri in seen:
                        continue
                    seen.add(iri)
                    label = None
                    for lit in context.objects(subj, RDFS.label):
                        if isinstance(lit, Literal):
                            label = str(lit)
                            break
                    comment = None
                    for lit in context.objects(subj, RDFS.comment):
                        if isinstance(lit, Literal):
                            comment = str(lit)
                            break
                    concepts.append(
                        OntologyConcept(
                            iri=iri,
                            label=label or _local_name(iri),
                            comment=comment,
                            concept_type="class" if cls_type != SKOS.Concept else "concept",
                            source_context=source,
                        )
                    )
            if concepts:
                concepts.sort(key=lambda c: (c.label or c.iri).lower())
                grouped[source] = concepts
        return grouped

    def get_properties_grouped(self) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for context in self._graph.contexts():
            if not hasattr(context, "identifier"):
                continue
            context_id = context.identifier
            if not isinstance(context_id, URIRef):
                continue
            context_name = str(context_id)
            source = _extract_source_context(context_name) or "Unassigned"
            props: List[Dict[str, Any]] = []
            seen: set[str] = set()
            type_map = {
                OWL.ObjectProperty: "object",
                OWL.DatatypeProperty: "datatype",
                RDF.Property: "annotation",
            }
            for rdf_type, property_type in type_map.items():
                for subj in context.subjects(RDF.type, rdf_type):
                    if not isinstance(subj, URIRef):
                        continue
                    iri = str(subj)
                    if iri in seen:
                        continue
                    seen.add(iri)
                    label = None
                    for lit in context.objects(subj, RDFS.label):
                        if isinstance(lit, Literal):
                            label = str(lit)
                            break
                    comment = None
                    for lit in context.objects(subj, RDFS.comment):
                        if isinstance(lit, Literal):
                            comment = str(lit)
                            break
                    domain = next(
                        (str(o) for o in context.objects(subj, RDFS.domain) if isinstance(o, URIRef)),
                        None,
                    )
                    range_ = next(
                        (str(o) for o in context.objects(subj, RDFS.range) if isinstance(o, URIRef)),
                        None,
                    )
                    props.append(
                        {
                            "iri": iri,
                            "label": label or _local_name(iri),
                            "concept_type": "property",
                            "property_type": property_type,
                            "domain": domain,
                            "range": range_,
                            "comment": comment,
                            "source_context": source,
                            "parent_concepts": [],
                            "child_concepts": [],
                        }
                    )
            if props:
                props.sort(key=lambda p: (p.get("label") or p["iri"]).lower())
                grouped[source] = props
        return grouped

    @staticmethod
    def _detect_concept_type(context, concept_uri: URIRef) -> tuple[str, Optional[str]]:
        types = {str(t) for t in context.objects(concept_uri, RDF.type)}
        if str(OWL.ObjectProperty) in types:
            return "property", "object"
        if str(OWL.DatatypeProperty) in types:
            return "property", "datatype"
        if str(OWL.AnnotationProperty) in types or str(RDF.Property) in types:
            return "property", "annotation"
        if str(SKOS.Concept) in types:
            return "concept", None
        if str(OWL.Class) in types or str(RDFS.Class) in types:
            return "class", None
        return "individual", None

    def get_concept_details(self, concept_iri: str) -> Optional[OntologyConcept]:
        """Return details for a concept IRI from the in-memory graph."""
        concept_uri = URIRef(concept_iri)
        for context in self._graph.contexts():
            if not hasattr(context, "identifier"):
                continue
            if (concept_uri, None, None) not in context:
                continue
            context_name = str(context.identifier)
            source = _extract_source_context(context_name)

            labels = list(context.objects(concept_uri, RDFS.label))
            labels.extend(list(context.objects(concept_uri, SKOS.prefLabel)))
            label = str(labels[0]) if labels else _local_name(concept_iri)

            comments = list(context.objects(concept_uri, RDFS.comment))
            comments.extend(list(context.objects(concept_uri, SKOS.definition)))
            comment = str(comments[0]) if comments else None

            concept_type, property_type = self._detect_concept_type(context, concept_uri)

            parent_concepts: List[str] = []
            for parent in context.objects(concept_uri, RDFS.subClassOf):
                if isinstance(parent, URIRef):
                    parent_concepts.append(str(parent))
            for parent in context.objects(concept_uri, SKOS.broader):
                if isinstance(parent, URIRef):
                    parent_concepts.append(str(parent))

            child_concepts: List[str] = []
            for child in context.subjects(RDFS.subClassOf, concept_uri):
                if isinstance(child, URIRef):
                    child_concepts.append(str(child))
            for child in context.subjects(SKOS.broader, concept_uri):
                if isinstance(child, URIRef):
                    child_concepts.append(str(child))

            related = [str(o) for o in context.objects(concept_uri, SKOS.related) if isinstance(o, URIRef)]
            synonyms = [str(o) for o in context.objects(concept_uri, SKOS.altLabel) if isinstance(o, Literal)]
            domain = next(
                (str(o) for o in context.objects(concept_uri, RDFS.domain) if isinstance(o, URIRef)),
                None,
            )
            range_ = next(
                (str(o) for o in context.objects(concept_uri, RDFS.range) if isinstance(o, URIRef)),
                None,
            )

            return OntologyConcept(
                iri=concept_iri,
                label=label,
                comment=comment,
                concept_type=concept_type,
                property_type=property_type,
                source_context=source,
                parent_concepts=parent_concepts,
                child_concepts=child_concepts,
                related_concepts=related,
                synonyms=synonyms,
                domain=domain,
                range=range_,
            )
        return None

    def get_concept_hierarchy(self, concept_iri: str):
        from src.models.ontology import ConceptHierarchy

        concept = self.get_concept_details(concept_iri)
        if not concept:
            return None
        ancestors = [
            self.get_concept_details(iri) or OntologyConcept(iri=iri, concept_type="class")
            for iri in concept.parent_concepts
        ]
        descendants = [
            self.get_concept_details(iri) or OntologyConcept(iri=iri, concept_type="class")
            for iri in concept.child_concepts
        ]
        return ConceptHierarchy(
            concept=concept,
            ancestors=[a for a in ancestors if a],
            descendants=[d for d in descendants if d],
            siblings=[],
        )

    def list_models(self, db=None, **_) -> List[Dict[str, Any]]:
        return self._semantic.search_triples("", limit=200)

    def save_ontology_bytes(self, filename: str, content: bytes) -> str:
        path = self._semantic.save_ontology_file(filename, content)
        try:
            context_name = f"urn:semantic-model:{Path(filename).stem}"
            ctx = self._graph.get_context(URIRef(context_name))
            for triple in list(ctx):
                ctx.remove(triple)
            tmp = Graph()
            tmp.parse(data=content.decode("utf-8", errors="ignore"), format="turtle")
            for triple in tmp:
                ctx.add(triple)
        except Exception as exc:
            logger.warning("Saved ontology file but failed to load into graph: %s", exc)
        return path

    def merge_triples(self, triples: List[Dict[str, str]]) -> int:
        return self._semantic.merge_triples(triples)

    def search_concepts(self, prefix: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        return self._semantic.search_triples(prefix, limit=limit)

    def append_job_result(
        self,
        table_name: str,
        *,
        status: str,
        parent_id: str,
        results: Dict[str, Any],
    ) -> str:
        return self._semantic.append_job_result(
            table_name,
            status=status,
            parent_id=parent_id,
            results=results,
        )
