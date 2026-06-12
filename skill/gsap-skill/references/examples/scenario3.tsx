import { useEffect, useRef, useState } from 'react';
import gsap from 'gsap';
import { useGSAP } from '@gsap/react';

gsap.registerPlugin(useGSAP);

type Item = {
  id: number;
  title: string;
  description: string;
};

const CardList = () => {
  const [items, setItems] = useState<Item[]>([]);
  const containerRef = useRef<HTMLUListElement>(null);

  // Simulate an async fetch: 50 items arrive after a 500ms delay.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const loaded: Item[] = Array.from({ length: 50 }, (_, i) => ({
        id: i + 1,
        title: `Card ${i + 1}`,
        description: `Description for card ${i + 1}`,
      }));
      setItems(loaded);
    }, 500);

    return () => {
      window.clearTimeout(timer);
    };
  }, []);

  useGSAP(() => {
    if (items.length === 0) return;

    gsap.from('.card', {
      opacity: 0,
      y: 30,
      duration: 0.5,
      ease: 'power2.out',
      stagger: 0.05,
    });
  }, {
    scope: containerRef,
    dependencies: [items.length],
    revertOnUpdate: true,
  });

  return (
    <ul ref={containerRef} className="card-list">
      {items.map((item) => (
        <li key={item.id} className="card">
          <h3>{item.title}</h3>
          <p>{item.description}</p>
        </li>
      ))}
    </ul>
  );
};

export default CardList;
