package dev.tenohost.combatbasics;

import org.bukkit.Bukkit;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.block.Block;
import org.bukkit.entity.Display;
import org.bukkit.entity.ItemDisplay;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.player.PlayerInteractEntityEvent;
import org.bukkit.event.world.ChunkLoadEvent;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.PlayerInventory;
import org.bukkit.persistence.PersistentDataContainer;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.NamespacedKey;
import org.bukkit.util.Transformation;
import org.bukkit.util.Vector;
import org.joml.Quaternionf;
import org.joml.Vector3f;

import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

public final class CombatBasicsPlugin extends JavaPlugin implements Listener {
    private final Set<UUID> displays = new HashSet<>();
    private NamespacedKey shieldKey;
    private NamespacedKey vxKey;
    private NamespacedKey vyKey;
    private NamespacedKey vzKey;
    private NamespacedKey yawKey;
    private NamespacedKey pitchKey;
    private NamespacedKey rollKey;
    private NamespacedKey pickupAtKey;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        shieldKey = new NamespacedKey(this, "shield-drop");
        vxKey = new NamespacedKey(this, "velocity-x");
        vyKey = new NamespacedKey(this, "velocity-y");
        vzKey = new NamespacedKey(this, "velocity-z");
        yawKey = new NamespacedKey(this, "rotation-yaw");
        pitchKey = new NamespacedKey(this, "rotation-pitch");
        rollKey = new NamespacedKey(this, "rotation-roll");
        pickupAtKey = new NamespacedKey(this, "pickup-at");
        getServer().getPluginManager().registerEvents(this, this);
        for (World world : Bukkit.getWorlds()) {
            world.getEntitiesByClass(ItemDisplay.class).stream()
                    .filter(this::isShieldDisplay)
                    .map(ItemDisplay::getUniqueId)
                    .forEach(displays::add);
        }
        getServer().getScheduler().runTaskTimer(this, this::tickDisplays, 1L, 1L);
    }

    @EventHandler(priority = EventPriority.HIGHEST, ignoreCancelled = true)
    public void onBlockedAttack(EntityDamageByEntityEvent event) {
        if (!getConfig().getBoolean("enabled")
                || !(event.getDamager() instanceof Player attacker)
                || !(event.getEntity() instanceof Player blocker)
                || !isHoe(attacker.getInventory().getItemInMainHand().getType())
                || !blocker.isBlocking()
                || !isFacingAttacker(blocker, attacker)) {
            return;
        }

        PlayerInventory inventory = blocker.getInventory();
        boolean offHand = inventory.getItemInOffHand().getType() == Material.SHIELD;
        ItemStack heldShield = offHand ? inventory.getItemInOffHand() : inventory.getItemInMainHand();
        if (heldShield.getType() != Material.SHIELD) {
            return;
        }

        ItemStack shield = heldShield.clone();
        Location location = blocker.getLocation().add(0, 1.0, 0);
        Vector direction = blocker.getLocation().toVector()
                .subtract(attacker.getLocation().toVector()).setY(0);
        if (direction.lengthSquared() < 0.0001) {
            direction = blocker.getLocation().getDirection().setY(0);
        }
        direction.normalize().multiply(getConfig().getDouble("launch-horizontal", 0.28));
        Vector velocity = direction.setY(getConfig().getDouble("launch-vertical", 0.34));
        ItemDisplay display = blocker.getWorld().spawn(location, ItemDisplay.class, entity -> {
            entity.setItemStack(shield);
            entity.setBillboard(Display.Billboard.FIXED);
            entity.setPersistent(true);
            entity.setGravity(false);
            entity.setInvulnerable(true);
            entity.setSilent(true);
            entity.setInterpolationDuration(0);
            entity.getPersistentDataContainer().set(shieldKey, PersistentDataType.BYTE, (byte) 1);
            set(entity.getPersistentDataContainer(), vxKey, velocity.getX());
            set(entity.getPersistentDataContainer(), vyKey, velocity.getY());
            set(entity.getPersistentDataContainer(), vzKey, velocity.getZ());
            set(entity.getPersistentDataContainer(), yawKey, Math.random() * Math.PI * 2);
            set(entity.getPersistentDataContainer(), pitchKey, Math.random() * Math.PI);
            set(entity.getPersistentDataContainer(), rollKey, 0.0);
            entity.getPersistentDataContainer().set(pickupAtKey, PersistentDataType.LONG,
                    getServer().getCurrentTick() + getConfig().getLong("pickup-delay-ticks", 10));
        });

        if (offHand) {
            inventory.setItemInOffHand(null);
        } else {
            inventory.setItemInMainHand(null);
        }
        displays.add(display.getUniqueId());
        blocker.sendMessage("§e¡Tu escudo fue desarmado!");
    }

    @EventHandler
    public void onPickupClick(PlayerInteractEntityEvent event) {
        if (!(event.getRightClicked() instanceof ItemDisplay display) || !isShieldDisplay(display)) {
            return;
        }
        event.setCancelled(true);
        pickup(event.getPlayer(), display);
    }

    @EventHandler
    public void onChunkLoad(ChunkLoadEvent event) {
        for (var entity : event.getChunk().getEntities()) {
            if (entity instanceof ItemDisplay display && isShieldDisplay(display)) {
                displays.add(display.getUniqueId());
            }
        }
    }

    private void tickDisplays() {
        for (UUID id : Set.copyOf(displays)) {
            if (!(Bukkit.getEntity(id) instanceof ItemDisplay display) || !display.isValid()) {
                displays.remove(id);
                continue;
            }
            if (!isShieldDisplay(display)) {
                displays.remove(id);
                continue;
            }
            tickDisplay(display);
        }
    }

    private void tickDisplay(ItemDisplay display) {
        Location current = display.getLocation();
        World world = display.getWorld();
        Vector velocity = new Vector(
                get(display.getPersistentDataContainer(), vxKey),
                get(display.getPersistentDataContainer(), vyKey),
                get(display.getPersistentDataContainer(), vzKey));
        double gravity = getConfig().getDouble("gravity-per-tick", 0.04);
        velocity.setY(velocity.getY() - gravity);

        Location horizontal = current.clone().add(velocity.getX(), 0, velocity.getZ());
        if (!world.isChunkLoaded(horizontal.getBlockX() >> 4, horizontal.getBlockZ() >> 4)) {
            return;
        }
        if (isSolid(world, horizontal)) {
            velocity.setX(-velocity.getX() * 0.45);
            velocity.setZ(-velocity.getZ() * 0.45);
        } else {
            current.setX(horizontal.getX());
            current.setZ(horizontal.getZ());
        }

        Location vertical = current.clone().add(0, velocity.getY(), 0);
        if (!world.isChunkLoaded(vertical.getBlockX() >> 4, vertical.getBlockZ() >> 4)) {
            return;
        }
        if (isSolid(world, vertical)
                || velocity.getY() < 0 && isSolid(world, vertical.clone().add(0, -0.35, 0))) {
            double damping = getConfig().getDouble("bounce-damping", 0.55);
            velocity.setY(velocity.getY() < 0 ? Math.abs(velocity.getY()) * damping
                    : -velocity.getY() * damping);
            if (Math.abs(velocity.getY()) < 0.08) {
                velocity.setY(0);
            }
        } else {
            current.setY(vertical.getY());
        }

        if (velocity.getY() == 0) {
            velocity.setX(velocity.getX() * 0.9);
            velocity.setZ(velocity.getZ() * 0.9);
        }
        display.teleport(current);
        set(display.getPersistentDataContainer(), vxKey, velocity.getX());
        set(display.getPersistentDataContainer(), vyKey, velocity.getY());
        set(display.getPersistentDataContainer(), vzKey, velocity.getZ());

        PersistentDataContainer data = display.getPersistentDataContainer();
        double yaw = get(data, yawKey) + 0.13;
        double pitch = get(data, pitchKey) + 0.07;
        double roll = get(data, rollKey) + 0.11;
        set(data, yawKey, yaw);
        set(data, pitchKey, pitch);
        set(data, rollKey, roll);
        display.setTransformation(new Transformation(
                new Vector3f(), new Quaternionf().rotateXYZ((float) pitch, (float) yaw, (float) roll),
                new Vector3f(0.75f), new Quaternionf()));

        long pickupAt = data.getOrDefault(pickupAtKey, PersistentDataType.LONG, 0L);
        if (getServer().getCurrentTick() >= pickupAt) {
            for (var entity : world.getNearbyEntities(current, 0.8, 0.8, 0.8)) {
                if (entity instanceof Player player && pickup(player, display)) {
                    break;
                }
            }
        }
    }

    private boolean pickup(Player player, ItemDisplay display) {
        if (!display.isValid() || !isShieldDisplay(display)
                || getServer().getCurrentTick() < display.getPersistentDataContainer()
                .getOrDefault(pickupAtKey, PersistentDataType.LONG, 0L)) {
            return false;
        }
        ItemStack item = display.getItemStack().clone();
        Map<Integer, ItemStack> remaining = player.getInventory().addItem(item);
        if (!remaining.isEmpty()) {
            return false;
        }
        display.remove();
        displays.remove(display.getUniqueId());
        player.playSound(player.getLocation(), org.bukkit.Sound.ENTITY_ITEM_PICKUP, 0.5f, 1.2f);
        return true;
    }

    private boolean isFacingAttacker(Player blocker, Player attacker) {
        Vector facing = blocker.getLocation().getDirection().setY(0);
        Vector toAttacker = attacker.getLocation().toVector()
                .subtract(blocker.getLocation().toVector()).setY(0);
        if (facing.lengthSquared() == 0 || toAttacker.lengthSquared() == 0) {
            return false;
        }
        return facing.normalize().dot(toAttacker.normalize()) >= 0.0;
    }

    private boolean isSolid(World world, Location location) {
        Block block = world.getBlockAt(location);
        return !block.isPassable();
    }

    private boolean isHoe(Material material) {
        return material.name().endsWith("_HOE");
    }

    private boolean isShieldDisplay(ItemDisplay display) {
        return display.getPersistentDataContainer().has(shieldKey, PersistentDataType.BYTE);
    }

    private void set(PersistentDataContainer data, NamespacedKey key, double value) {
        data.set(key, PersistentDataType.DOUBLE, value);
    }

    private double get(PersistentDataContainer data, NamespacedKey key) {
        return data.getOrDefault(key, PersistentDataType.DOUBLE, 0.0);
    }
}
