"""
Embedding Engine - Text Vectorization
嵌入引擎 - 文本向量化

Features:
- Mock embedding fallback
- Local cache
- Batch processing
"""

import json
import hashlib
from pathlib import Path
from typing import List, Optional
import logging

logger = logging.getLogger(__name__)


class EmbeddingEngine:
    """
    Embedding engine with local cache and mock fallback.
    """
    
    def __init__(self, cache_dir: Optional[Path] = None):
        if cache_dir is None:
            cache_dir = Path(__file__).parent.parent / "data" / "embeddings"
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._use_mock = True  # Default to mock
    
    def _get_cache_key(self, text: str) -> str:
        """Generate cache key from text"""
        return hashlib.md5(text.encode()).hexdigest()
    
    def _get_cache_path(self, key: str) -> Path:
        """Get cache file path"""
        return self.cache_dir / f"{key}.json"
    
    def _load_from_cache(self, text: str) -> Optional[List[float]]:
        """Try to load embedding from cache"""
        key = self._get_cache_key(text)
        cache_path = self._get_cache_path(key)
        
        if cache_path.exists():
            try:
                with open(cache_path, 'r') as f:
                    data = json.load(f)
                return data.get('embedding')
            except Exception as e:
                logger.warning(f"Failed to load cache: {e}")
        return None
    
    def _save_to_cache(self, text: str, embedding: List[float]):
        """Save embedding to cache"""
        key = self._get_cache_key(text)
        cache_path = self._get_cache_path(key)
        
        try:
            with open(cache_path, 'w') as f:
                json.dump({'text': text[:100], 'embedding': embedding}, f)
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")
    
    def embed(self, text: str) -> List[float]:
        """
        Get embedding for text.
        Uses mock embedding (hash-based) as fallback.
        """
        # Try cache first
        cached = self._load_from_cache(text)
        if cached:
            return cached
        
        # Generate mock embedding (deterministic)
        embedding = self._mock_embed(text)
        
        # Cache it
        self._save_to_cache(text, embedding)
        
        return embedding
    
    def _mock_embed(self, text: str, dim: int = 1536) -> List[float]:
        """
        Generate deterministic mock embedding.
        Uses hash of text to generate vector.
        """
        import random
        seed = int(hashlib.md5(text.encode()).hexdigest(), 16) % (2**32)
        rng = random.Random(seed)
        return [rng.uniform(-1, 1) for _ in range(dim)]
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Embed multiple texts"""
        return [self.embed(t) for t in texts]


# Singleton
_engine = None


def get_embedding_engine() -> EmbeddingEngine:
    """Get singleton EmbeddingEngine instance"""
    global _engine
    if _engine is None:
        _engine = EmbeddingEngine()
    return _engine
